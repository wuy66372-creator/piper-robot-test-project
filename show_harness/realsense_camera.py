"""Serial-bound RealSense RGB/depth front view shared by GUMI and inference."""
import threading
import time
import logging

import cv2
import numpy as np


class RealSenseFrontStore:
    max_age_s = .8
    max_bytes = 800_000
    token = None

    def __init__(self, serial=None, clock=time.monotonic, depth_enabled=False):
        self.clock = clock
        self.serial = serial
        self.depth_enabled = depth_enabled
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.context = None
        self.rgb = self.jpeg = self.depth = None
        self.captured = 0.
        self.sequence = -1
        self.error = None
        self.details = {}

    def connect(self):
        import pyrealsense2 as rs
        self.context = rs.context()
        devices = [d for d in self.context.query_devices()
                   if 'D435I' in d.get_info(rs.camera_info.name).upper()]
        if self.serial:
            devices = [d for d in devices if d.get_info(rs.camera_info.serial_number) == self.serial]
        if len(devices) != 1:
            raise RuntimeError('Expected one D435i; connect the camera or specify --front-serial')
        device = devices[0]
        self.serial = device.get_info(rs.camera_info.serial_number)
        self.details = {'serial': self.serial, 'name': device.get_info(rs.camera_info.name),
                        'firmware': device.get_info(rs.camera_info.firmware_version),
                        'usb_type': device.get_info(rs.camera_info.usb_type_descriptor)}
        self.thread = threading.Thread(target=self._capture, name='realsense-front', daemon=True)
        self.thread.start()
        if not self.ready.wait(20):
            reason = self.error or 'no fresh camera frames'
            self.close()
            raise RuntimeError(f'D435i failed to start: {reason}')
        return self

    def _publish(self, rgb, depth, sequence, captured):
        """Reject stale/non-advancing captures before exposing either stream."""
        if not 0 <= self.clock() - captured <= self.max_age_s:
            return False
        ok, encoded = cv2.imencode('.jpg', cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                                   [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise RuntimeError('D435i JPEG encoding failed')
        with self.lock:
            if sequence <= self.sequence:
                return False
            self.rgb = rgb.copy()
            self.depth = None if depth is None else depth.copy()
            self.jpeg = encoded.tobytes()
            self.sequence, self.captured = sequence, captured
            self.error = None
        self.ready.set()
        return True

    def _capture(self):
        import pyrealsense2 as rs
        while not self.closed.is_set():
            pipeline = rs.pipeline(self.context)
            started = False
            try:
                cfg = rs.config()
                cfg.enable_device(self.serial)
                cfg.enable_stream(rs.stream.color, 640, 480, rs.format.rgb8, 15)
                if self.depth_enabled:
                    cfg.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 15)
                # Use SDK host/global timestamps so a queued old frame cannot
                # be relabelled as fresh when the capture thread resumes.
                device = cfg.resolve(rs.pipeline_wrapper(pipeline)).get_device()
                for sensor in device.query_sensors():
                    if sensor.supports(rs.option.global_time_enabled):
                        sensor.set_option(rs.option.global_time_enabled, 1)
                    if sensor.supports(rs.option.frames_queue_size):
                        sensor.set_option(rs.option.frames_queue_size, 1)
                profile = pipeline.start(cfg)
                started = True
                scale = profile.get_device().first_depth_sensor().get_depth_scale() if self.depth_enabled else None
                intr = profile.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()
                with self.lock:
                    self.sequence = -1
                    self.details.update(depth_scale_m=scale, fps=15,
                                        intrinsics={'fx': intr.fx, 'fy': intr.fy, 'ppx': intr.ppx,
                                                    'ppy': intr.ppy, 'coeffs': list(intr.coeffs)})
                align = rs.align(rs.stream.color) if self.depth_enabled else None
                while not self.closed.is_set():
                    frames = pipeline.wait_for_frames(5000)
                    color = frames.get_color_frame()
                    if not color:
                        continue
                    if color.get_frame_timestamp_domain() not in (rs.timestamp_domain.global_time,
                                                                  rs.timestamp_domain.system_time):
                        continue  # global clock may need a short warm-up
                    age = time.time() - color.get_timestamp() / 1000
                    if not -.05 <= age <= self.max_age_s:
                        continue
                    captured = self.clock() - max(0., age)
                    depth = None
                    if align:
                        depth_frame = align.process(frames).get_depth_frame()
                        if not depth_frame:
                            continue
                        depth = np.asanyarray(depth_frame.get_data())
                    self._publish(np.asanyarray(color.get_data()), depth,
                                  color.get_frame_number(), captured)
            except Exception as exc:
                logging.getLogger(__name__).warning('D435i capture failed; retrying: %s', exc, exc_info=True)
                with self.lock:
                    self.error = str(exc)
                    self.rgb = self.jpeg = self.depth = None
            finally:
                if started:
                    pipeline.stop()
            self.closed.wait(2)

    def frame(self):
        with self.lock:
            age = self.clock() - self.captured
            meta = dict(self.details, available=False, source='realsense_d435i',
                        synchronized_with_wrists=False)
            if self.error:
                meta['error'] = self.error
            if self.closed.is_set() or self.rgb is None or not 0 <= age <= self.max_age_s:
                return None, None, meta
            meta.update(available=True, sequence=self.sequence, age_upper_bound_s=age,
                        width=self.rgb.shape[1], height=self.rgb.shape[0], depth_available=self.depth is not None,
                        depth_aligned_to_color=self.depth is not None)
            return self.rgb.copy(), self.jpeg, meta

    def state(self):
        return self.frame()[2]

    def start(self, client):
        raise RuntimeError('Front camera is the host D435i; browser upload is disabled')

    def put(self, *args, **kwargs):
        raise RuntimeError('Front camera is the host D435i; browser upload is disabled')

    def stop(self, token):
        return False  # browsers cannot shut down the host camera

    def close(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=5)
        with self.lock:
            self.rgb = self.jpeg = self.depth = None
