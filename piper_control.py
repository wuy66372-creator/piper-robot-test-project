#!/usr/bin/env python3
"""Local AGILE X Piper commissioning; status is receive-only by default."""
import argparse
import collections
import datetime
import fcntl
import json
import math
from pathlib import Path
import select
import socket
import struct
import sys
import time

CAN0_SERIAL = '0040004B4759530820353131'
CAN1_SERIAL = '0039002C4759530920353131'
CONTROL_SERIALS = {'can0': CAN0_SERIAL, 'can1': CAN1_SERIAL}
REQUIRED = (0x2A1, 0x2A5, 0x2A6, 0x2A7, *range(0x261, 0x267))


class Feedback:
    def __init__(self):
        self.frames = {}
        self.counts = collections.Counter()
        self.limits = {}
        self.firmware = bytearray()

    def accept(self, cid, data, now):
        if cid & ~0x7FF:
            return
        self.counts[cid] += 1
        if len(data) != 8:
            return
        self.frames[cid] = (bytes(data), now)
        if cid == 0x473 and 1 <= data[0] <= 6:
            maximum, minimum, speed = struct.unpack('>hhH', data[1:7])
            self.limits[data[0]] = (minimum * 100, maximum * 100, speed)
        if cid == 0x4AF:
            self.firmware.extend(data)

    def fresh(self, now):
        for cid in REQUIRED:
            age_limit = .3 if 0x261 <= cid <= 0x266 else .15
            if cid not in self.frames or now - self.frames[cid][1] > age_limit:
                raise RuntimeError(f'missing/stale standard feedback 0x{cid:X}')

    def joints(self):
        return [q for cid in (0x2A5, 0x2A6, 0x2A7)
                for q in struct.unpack('>ii', self.frames[cid][0])]

    def healthy(self, now, enabled=False):
        self.fresh(now)
        status = self.frames[0x2A1][0]
        if status[1] or status[6] or status[7]:
            raise RuntimeError('arm fault/emergency stop; no automatic reset')
        for cid in range(0x261, 0x267):
            flags = self.frames[cid][0][5]
            if flags & 0xBF:
                raise RuntimeError(f'driver fault 0x{cid:X}: 0x{flags:X}')
            if enabled and not flags & 0x40:
                raise RuntimeError('motors are not all enabled; no automatic enable')

    def validate_target(self, target):
        if len(target) != 6 or set(self.limits) != set(range(1, 7)):
            raise RuntimeError('six distinct hardware joint limits required')
        for i, q in enumerate(target, 1):
            lo, hi, speed = self.limits[i]
            if lo >= hi or speed <= 0 or not lo <= q <= hi:
                raise RuntimeError(f'joint {i} target {q / 1000} outside valid hardware limits')

    def snapshot(self):
        now = time.monotonic()
        out = {'ids': {f'0x{k:X}': v for k, v in sorted(self.counts.items())},
               'hardware_limits': {str(i): {'min_deg': a / 1000, 'max_deg': b / 1000,
                                           'max_speed_rad_s': c / 1000}
                                   for i, (a, b, c) in self.limits.items()}}
        if all(c in self.frames and now - self.frames[c][1] <= .3 for c in range(0x261, 0x267)):
            out['enabled'] = [bool(self.frames[c][0][5] & 64) for c in range(0x261, 0x267)]
            out['driver_voltage_V'] = [int.from_bytes(self.frames[c][0][:2], 'big') / 10
                                       for c in range(0x261, 0x267)]
        try:
            self.fresh(now)
            s = self.frames[0x2A1][0]
            out.update(standard_feedback=True, ctrl_mode=s[0], arm_status=s[1],
                       teach_status=s[3],
                       fault_code=int.from_bytes(s[6:8], 'big'),
                       joints_deg=[q / 1000 for q in self.joints()],
                       enabled=[bool(self.frames[c][0][5] & 64) for c in range(0x261, 0x267)])
        except RuntimeError as e:
            out.update(standard_feedback=False, reason=str(e))
        start = self.firmware.find(b'S-V')
        if start >= 0:
            out['firmware'] = self.firmware[start:start + 8].decode('ascii')
        return out


class Receiver:
    def __init__(self, interface):
        self.feedback = Feedback()
        self.socket = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        self.socket.bind((interface,))

    def collect(self, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select([self.socket], [], [], max(0, deadline - time.monotonic()))[0]:
                raw, _, flags, _ = self.socket.recvmsg(16)
                if flags & socket.MSG_DONTROUTE:  # Ignore host TX echoed from the SDK socket.
                    continue
                cid, length, data = struct.unpack('=IB3x8s', raw)
                self.feedback.accept(cid, data[:length], time.monotonic())


def bounded_target(origin, joint, delta, speed):
    if not math.isfinite(delta) or abs(delta) > 2 or not 1 <= speed <= 5:
        raise ValueError('commissioning bound: delta <= 2 degrees, speed 1–5 percent')
    if not 1 <= joint <= 6:
        raise ValueError('joint must be 1–6')
    target = list(origin)
    target[joint - 1] += round(delta * 1000)
    return target


def verify_control_device(interface):
    expected = CONTROL_SERIALS.get(interface)
    if expected is None:
        raise RuntimeError('unregistered control interface')
    path = Path('/sys/class/net') / interface / 'device'
    for parent in (path.resolve(), *path.resolve().parents):
        serial = parent / 'serial'
        if serial.exists():
            if serial.read_text().strip() == expected:
                return
            break
    raise RuntimeError('CAN adapter serial does not match the registered arm')


def check_mode_transition(feedback):
    status = feedback.frames[0x2A1][0]
    if status[0] not in (0, 1, 2):
        raise RuntimeError('arm is controlled by another mode; takeover refused')
    if status[0] == 2:
        if status[3] not in (0, 2):
            raise RuntimeError('end button teaching first; indicator must be off')
        q = feedback.joints()
        # Agilex-College playPos_new.py: |J2|, |J3| < .1745 rad;
        # .2094 < J5 < .7854 rad. This is a recovery posture gate,
        # not a requirement that all six encoder values be zero.
        if not (abs(q[1]) < 9998 and abs(q[2]) < 9998 and 11998 < q[4] < 45001):
            raise RuntimeError('teaching recovery posture required: |J2|/|J3| < 10 deg, J5 about 12–45 deg')
    if any(cid in feedback.counts for cid in range(0x150, 0x160)):
        raise RuntimeError('another controller is sending motion frames on the bus')


def sdk_connect(interface):
    from piper_sdk import C_PiperInterface_V2
    sdk = C_PiperInterface_V2(interface, start_sdk_joint_limit=False)
    # Native receiver tracks every segment independently; avoid SDK default data/cache aliasing.
    sdk.ConnectPort(piper_init=False, start_thread=False)
    return sdk


def query_limits(sdk, receiver):
    for i in range(1, 7):
        sdk.SearchMotorMaxAngleSpdAccLimit(i, 1)
        receiver.collect(.07)
    if set(receiver.feedback.limits) != set(range(1, 7)):
        raise RuntimeError('incomplete hardware limit responses')


def drive(sdk, receiver, target, origin, seconds, speed):
    f = receiver.feedback
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        receiver.collect(.02)
        f.healthy(time.monotonic(), enabled=True)
        if any(cid in f.counts for cid in range(0x150, 0x160)):
            raise RuntimeError('another controller is sending motion frames on the bus')
        if any(abs(a - b) > 3500 for a, b in zip(f.joints(), origin)):
            raise RuntimeError('movement exceeded commissioning envelope')
        f.validate_target(target)
        sdk.JointCtrl(*target)
        sdk.MotionCtrl_2(0x01, 0x01, speed)
    receiver.collect(.05)
    f.healthy(time.monotonic(), enabled=True)
    if f.frames[0x2A1][0][0] != 1:
        raise RuntimeError('CAN control mode not acknowledged by fresh feedback')
    if max(abs(a - b) for a, b in zip(f.joints(), target)) > 500:
        raise RuntimeError('target not reached within 0.5 degree tolerance')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['status', 'firmware', 'limits', 'hold', 'jog', 'stop'])
    p.add_argument('--can', default='can0')
    p.add_argument('--joint', type=int, default=6)
    p.add_argument('--delta', type=float, default=1)
    p.add_argument('--speed', type=int, default=5)
    p.add_argument('--execute', action='store_true', help='send physical control commands')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    result = {'checked_at': datetime.datetime.now().astimezone().isoformat(),
              'interface': args.can, 'command': args.command, 'executed': False}
    sdk = receiver = None
    active = False
    # Serialize this tool's transmitters while allowing passive status probes.
    lock_path = Path.home() / '.cache/hackathon/piper-control.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            if args.command != 'status':
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command in ('hold', 'jog', 'stop'):
                verify_control_device(args.can)
            receiver = Receiver(args.can)
            receiver.collect(.4)
            f = receiver.feedback
            result['before'] = f.snapshot()
            if args.command == 'status':
                result['ok'] = bool(f.counts)
            elif args.command == 'stop':
                if args.execute:
                    sdk = sdk_connect(args.can)
                    sdk.MotionCtrl_1(0x01, 0, 0)
                    result['executed'] = True
                    receiver.collect(.3)
                    f.fresh(time.monotonic())
                    if f.frames[0x2A1][0][1] != 1:
                        raise RuntimeError('emergency stop feedback not acknowledged')
                result['ok'] = True
            else:
                sdk = sdk_connect(args.can)
                if args.command == 'firmware':
                    sdk.SearchPiperFirmwareVersion()
                    receiver.collect(.5)
                    result['ok'] = 'firmware' in f.snapshot()
                else:
                    query_limits(sdk, receiver)
                    if args.command == 'limits':
                        result['ok'] = True
                    else:
                        f.healthy(time.monotonic(), enabled=True)
                        check_mode_transition(f)
                        origin = f.joints()
                        target = bounded_target(origin, args.joint,
                                                args.delta if args.command == 'jog' else 0,
                                                args.speed)
                        f.validate_target(origin)
                        f.validate_target(target)
                        result['plan'] = {'origin_deg': [x / 1000 for x in origin],
                                          'target_deg': [x / 1000 for x in target],
                                          'return_to_origin': args.command == 'jog',
                                          'speed_percent': args.speed}
                        if args.execute:
                            receiver.collect(.2)
                            f.healthy(time.monotonic(), enabled=True)
                            check_mode_transition(f)
                            if max(abs(a - b) for a, b in zip(f.joints(), origin)) > 200:
                                raise RuntimeError('arm is moving/being dragged; takeover refused')
                            active = True  # Includes the first partial send in error handling.
                            result['executed'] = True
                            # Official manual: end button teaching, recovery posture,
                            # standby, then CAN control. No reset or auto-homing.
                            if f.frames[0x2A1][0][0] == 2:
                                sdk.MotionCtrl_2(0x00, 0x01, 0)
                                receiver.collect(.1)
                                f.healthy(time.monotonic(), enabled=True)
                                if f.frames[0x2A1][0][0] != 0:
                                    raise RuntimeError('standby mode transition rejected')
                            # Enter joint control at zero commanded speed, then seed the
                            # latest pose before raising speed. Never reuse an old target.
                            sdk.MotionCtrl_2(0x01, 0x01, 0)
                            receiver.collect(.05)
                            f.healthy(time.monotonic(), enabled=True)
                            if f.frames[0x2A1][0][0] != 1:
                                raise RuntimeError('CAN control mode transition rejected')
                            origin = f.joints()
                            target = bounded_target(origin, args.joint,
                                                    args.delta if args.command == 'jog' else 0,
                                                    args.speed)
                            f.validate_target(origin)
                            f.validate_target(target)
                            sdk.JointCtrl(*origin)
                            result['plan']['origin_deg'] = [x / 1000 for x in origin]
                            result['plan']['target_deg'] = [x / 1000 for x in target]
                            drive(sdk, receiver, origin, origin, 1, args.speed)
                            drive(sdk, receiver, target, origin, 3, args.speed)
                            result['at_target'] = f.snapshot()
                            if args.command == 'jog':
                                drive(sdk, receiver, origin, origin, 3, args.speed)
                            active = False
                            result['executed'] = True
                        result['ok'] = True
        except (Exception, KeyboardInterrupt) as e:
            result.update(ok=False, error=str(e) or 'interrupted')
            if active and sdk:
                try:
                    sdk.MotionCtrl_1(0x01, 0, 0)
                    result['stop_sent_on_failure'] = True
                except Exception as stop_error:
                    result['stop_error'] = str(stop_error)
        finally:
            if receiver:
                result['after'] = receiver.feedback.snapshot()
                receiver.socket.close()
            if sdk:
                sdk.DisconnectPort()
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + '\n')
    print(encoded)
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
