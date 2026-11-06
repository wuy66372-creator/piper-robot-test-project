"""Machine-local Show-Harness transport. Connecting never enables or moves an arm."""
from pathlib import Path
import fcntl
import math
import struct
import subprocess
import threading
import time

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

import piper_control as pc

ROOT = Path(__file__).resolve().parents[1]
ARMS = {'left': 'can0', 'right': 'can1'}
SERIALS = {'/camera_l/color/image_raw': 'CC1365301R4',
           '/camera_r/color/image_raw': 'CC1365301HT'}
# Both site arms report max_range_config=100 via read-only 0x47E feedback.
# This is the configured stroke, not a request to probe a mechanical hard stop.
GRIPPER_MAX_WIDTH_M = .100


class CommandLease:
    mutex = threading.Lock()
    handle = None
    users = 0

    @classmethod
    def acquire(cls):
        with cls.mutex:
            if cls.handle is None:
                path = Path.home() / '.cache/hackathon/piper-control.lock'
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open('a')
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    handle.close()
                    raise
                cls.handle = handle
            cls.users += 1

    @classmethod
    def release(cls):
        with cls.mutex:
            cls.users -= 1
            if cls.users == 0:
                cls.handle.close()
                cls.handle = None


def target_units(positions, origin, limits):
    values = np.asarray(positions, dtype=float)
    if values.shape != (6,) or not np.isfinite(values).all():
        raise ValueError('six finite joint angles in radians required')
    target = [round(math.degrees(v) * 1000) for v in values]
    if max(abs(a-b) for a, b in zip(target, origin)) > 2000:
        raise RuntimeError('target exceeds session commissioning envelope of 2 degrees')
    limits.validate_target(target)
    return target


class LocalPiperInterface:
    allow_motion = False
    hardware_limits = {}

    def __init__(self, arm='left', open_width_m=GRIPPER_MAX_WIDTH_M, **kwargs):
        if arm not in ARMS:
            raise ValueError('arm must be left/right')
        self.arm = arm
        self.allow_motion = type(self).allow_motion
        self.interface = ARMS[arm]
        if not math.isfinite(open_width_m) or not 0 < open_width_m <= GRIPPER_MAX_WIDTH_M:
            raise ValueError('open gripper width must be finite and in (0, 100] mm')
        self.open_width_m = open_width_m
        self.receiver = self.sdk = self.thread = None
        self.mutex = threading.RLock()
        self.stop = threading.Event()
        self.error = None
        self.leased = False
        self.origin = None
        self.kin = None
        self.last_command = None

    def connect(self):
        if self.receiver is not None:
            return self
        pc.verify_control_device(self.interface)
        CommandLease.acquire()
        self.leased = True
        try:
            self.receiver = pc.Receiver(self.interface)
            self.receiver.collect(.4)
            self.receiver.feedback.healthy(time.monotonic())
            self.check_connect_mode(self.receiver.feedback)
            self.sdk = pc.sdk_connect(self.interface)
            pc.query_limits(self.sdk, self.receiver)  # queries only
            from core.piper import kinematics
            ranges = np.radians(np.array([[self.receiver.feedback.limits[i][0]/1000,
                                          self.receiver.feedback.limits[i][1]/1000]
                                         for i in range(1,7)]))
            # A shared upstream IK table must obey every connected arm's queried limits.
            type(self).hardware_limits[self.arm] = ranges
            all_ranges = list(type(self).hardware_limits.values())
            kinematics.JOINT_LIMITS_RAD = np.column_stack((
                np.max([r[:,0] for r in all_ranges],axis=0),
                np.min([r[:,1] for r in all_ranges],axis=0)))
            self.origin = self.receiver.feedback.joints()
            self.thread = threading.Thread(target=self._receive, daemon=True)
            self.thread.start()
            self.get_ee_pose()
            return self
        except BaseException:
            self.close()
            raise

    def check_connect_mode(self,feedback):
        pc.check_mode_transition(feedback)

    def _receive(self):
        try:
            while not self.stop.is_set():
                with self.mutex:
                    self.receiver.collect(.01)
                self.stop.wait(.001)
        except BaseException as exc:
            self.error = exc

    def _feedback(self):
        if self.error:
            raise RuntimeError('CAN receiver failed') from self.error
        if self.receiver is None:
            raise RuntimeError('not connected')
        feedback = self.receiver.feedback
        feedback.healthy(time.monotonic())
        return feedback

    def _frame(self, cid):
        f = self._feedback()
        if cid not in f.frames or time.monotonic()-f.frames[cid][1] > .15:
            raise RuntimeError(f'missing/stale feedback 0x{cid:X}')
        return f.frames[cid][0]

    def get_joint_positions(self):
        with self.mutex:
            return np.radians(np.array(self._feedback().joints()) / 1000)

    def get_joint_velocities(self):
        # Native high-speed messages encode joint speed in 0.001 rad/s.
        with self.mutex:
            return np.array([struct.unpack('>h', self._frame(c)[:2])[0] / 1000
                             for c in range(0x251, 0x257)])

    def get_ee_pose(self):
        with self.mutex:
            values = [v for cid in (0x2A2, 0x2A3, 0x2A4)
                      for v in struct.unpack('>ii', self._frame(cid))]
            xyz = np.array(values[:3]) / 1e6
            quat = Rotation.from_euler('xyz', np.radians(np.array(values[3:]) / 1000)).as_quat()
            return np.r_[xyz, quat]

    def get_force_torque(self):
        raise NotImplementedError('no measured wrist force/torque sensor is connected')

    def get_gripper_position(self):
        with self.mutex:
            width = struct.unpack('>i', self._frame(0x2A8)[:4])[0] / 1e6
            # Closed-jaw feedback can undershoot zero slightly (this rig: -0.1 mm).
            # Normalize only that bounded sensor tolerance; never change hardware zero.
            if not -.0002 <= width <= GRIPPER_MAX_WIDTH_M + .0002:
                raise RuntimeError('invalid gripper width')
            return np.array([max(0., width)])

    def arm_status(self):
        with self.mutex:
            return self._feedback().snapshot()

    def stream_joints(self, positions):
        with self.mutex:
            if not self.allow_motion:
                raise RuntimeError('read-only backend: motion is not enabled')
            f = self._feedback()
            f.healthy(time.monotonic(), enabled=True)
            if self._frame(0x2A1)[0] != 1:
                raise RuntimeError('requires an already enabled CAN-controlled arm')
            pc.check_mode_transition(f)
            if max(abs(a-b) for a,b in zip(f.joints(),self.origin)) > 3500:
                raise RuntimeError('measured motion exceeded commissioning envelope')
            target = target_units(positions, self.origin, f)
            self.sdk.JointCtrl(*target)
            self.sdk.MotionCtrl_2(0x01, 0x01, 5)
            self.last_command = np.array(positions, dtype=float)

    def move_to_joint_positions(self, positions, time_to_go):
        # Validate BEFORE emitting interpolation points; never partially execute an invalid goal.
        with self.mutex:
            target_units(positions, self.origin, self._feedback())
        start = self.get_joint_positions()
        duration = max(1., float(time_to_go))
        if not math.isfinite(duration) or duration > 30:
            raise ValueError('move duration must be finite and <=30 seconds')
        count = max(2, round(duration * 50))
        for i in range(1,count+1):
            self.stream_joints(start+(np.asarray(positions)-start)*i/count)
            time.sleep(duration/count)
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            if np.max(np.abs(self.get_joint_positions()-positions)) < math.radians(.5):
                return
            time.sleep(.02)
        raise RuntimeError('joint target did not converge')

    def update_desired_ee_pose(self, pose):
        # Do not bypass joint envelope via upstream's Cartesian fallback.
        raise RuntimeError('direct Cartesian fallback disabled; use validated joint-stream IK')

    def note_commanded_pose(self, pose):
        pass  # gripper commands never reuse a Cartesian setpoint in this backend

    def invalidate_commanded_pose(self):
        pass

    def set_gripper_position(self, pos):
        raise RuntimeError('gripper motion has not been commissioned through this adapter')

    def control_gripper(self, close):
        self.set_gripper_position(0 if close else self.open_width_m)

    def get_gripper_prev_cmd_success(self):
        return False

    def enable(self):
        raise RuntimeError('automatic enabling is disabled; use the commissioning tool')

    def disable(self):
        raise RuntimeError('automatic disabling is disabled; arm support must be managed onsite')

    def terminate_current_policy(self):
        pass  # no autonomous policy is started; last bounded position target remains active

    def start_cartesian_impedance(self, *args, **kwargs):
        pass

    def start_joint_impedance(self, *args, **kwargs):
        pass

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
        if self.receiver:
            self.receiver.socket.close()
            self.receiver = None
        if self.sdk:
            self.sdk.DisconnectPort()
            self.sdk = None
        if self.leased:
            CommandLease.release()
            self.leased = False


class WristPublisher:
    mutex = threading.Lock()
    process = directory = lease = None
    users = 0

    @classmethod
    def acquire(cls):
        import os
        import subprocess
        import tempfile
        with cls.mutex:
            if cls.process is None:
                path = Path.home() / '.cache/hackathon/orbbec-stream.lock'
                path.parent.mkdir(parents=True,exist_ok=True)
                lease = path.open('a')
                try:
                    fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    binary = Path(os.environ.get('XDG_CACHE_HOME',Path.home()/'.cache')) / 'hackathon/orbbec-v1.10.37/stream'
                    directory = tempfile.TemporaryDirectory(prefix='show-harness-cameras-')
                    cls.process = subprocess.Popen([str(binary),directory.name],cwd=binary.parent)
                    cls.directory = directory
                    cls.lease = lease
                except BaseException:
                    lease.close()
                    raise
            cls.users += 1
            return Path(cls.directory.name)

    @classmethod
    def release(cls):
        with cls.mutex:
            cls.users -= 1
            if cls.users == 0:
                cls.process.terminate()
                try:
                    cls.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cls.process.kill();cls.process.wait()
                cls.directory.cleanup()
                cls.lease.close()
                cls.process=cls.directory=cls.lease=None


class LocalRGBCamera:
    def __init__(self, topic, max_age_s=1., connect_timeout_s=10., **kwargs):
        if topic == '/camera_f/color/image_raw':
            from browser_camera import BrowserFrontCamera
            self.front = BrowserFrontCamera(max_age_s=max_age_s)
            return
        if topic not in SERIALS:
            raise RuntimeError('front camera is not configured; only two wrist cameras are connected')
        self.serial=SERIALS[topic]
        self.max_age_s=max_age_s
        self.directory=WristPublisher.acquire()
        self.closed=False
        deadline=time.monotonic()+connect_timeout_s
        while time.monotonic()<deadline:
            if self.read()[0]:
                return
            if WristPublisher.process.poll() is not None:
                self.release()
                raise RuntimeError('Orbbec publisher exited before first camera frame')
            time.sleep(.02)
        self.release()
        raise RuntimeError(f'no camera frames for {self.serial}')

    def read(self):
        if getattr(self, 'front', None) is not None:
            return self.front.read()
        import json
        if self.closed or WristPublisher.process.poll() is not None:
            return False,None,None
        try:
            meta=json.loads((self.directory/(self.serial+'.json')).read_text())
            age=time.monotonic()-meta['arrival_monotonic_s']
            if not 0 <= age <= self.max_age_s:
                return False,None,None
            bgr=cv2.imread(str(self.directory/meta['file']))
            if bgr is None:
                return False,None,None
            return True,cv2.cvtColor(bgr,cv2.COLOR_BGR2RGB),None
        except (OSError,ValueError,KeyError):
            return False,None,None

    def release(self):
        if getattr(self, 'front', None) is not None:
            self.front.release()
            return
        if not self.closed:
            self.closed=True
            WristPublisher.release()


def install(allow_motion=False):
    """Bind upstream sessions to local transports before constructing any session."""
    import core.piper.piper_session as single
    import core.piper.dual_session as dual
    LocalPiperInterface.allow_motion = allow_motion
    for module in (single,dual):
        module.PiperInterface = LocalPiperInterface
        module.RosImageCamera = LocalRGBCamera
