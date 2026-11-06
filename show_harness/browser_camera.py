"""Browser webcam frames with a server-issued, bounded-age capture ticket."""
import math
import secrets
import threading
import time

import cv2
import numpy as np
import requests


class BrowserFrontStore:
    max_age_s = .8
    max_bytes = 800_000

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.client = self.token = self.ticket = None
        self.ticket_time = self.valid_until = 0.
        self.sequence = -1
        self.media_time = -1.
        self.presented_frame = -1
        self.rgb = self.jpeg = None
        self.capture_lower_bound = 0.
        self.frame_sequence = -1

    def start(self, client):
        if not isinstance(client, str) or not 8 <= len(client) <= 128:
            raise ValueError('Invalid webcam client')
        with self.lock:
            now = self.clock()
            if self.client != client and now < self.valid_until:
                raise RuntimeError('Another browser is supplying the front camera')
            same_source = self.client == client
            self.client = client
            self.token = secrets.token_urlsafe(24)
            self.sequence = self.media_time = self.presented_frame = -1
            # Renewing a lost ticket must not introduce an artificial camera
            # gap. Preserve the last image only for its ORIGINAL age budget.
            if not same_source:
                self.rgb = self.jpeg = None
                self.frame_sequence = -1
            self.valid_until = now + self.max_age_s
            return self._ticket(now)

    def _ticket(self, now):
        self.ticket = secrets.token_urlsafe(24)
        self.ticket_time = now
        return {'ok': True, 'token': self.token, 'ticket': self.ticket,
                'max_age_ms': self.max_age_s * 1000}

    def put(self, body, token, ticket, sequence, media_time, frame=None):
        if not 0 < len(body) <= self.max_bytes:
            raise ValueError('Webcam image must be at most 800 KB')
        if not math.isfinite(media_time) or media_time < 0 or sequence < 0 or (frame is not None and frame < 0):
            raise ValueError('Invalid webcam frame counters')
        # Decode outside the lock; stopping a source must not wait for image decoding.
        bgr = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None or not (120 <= bgr.shape[0] <= 960 and 160 <= bgr.shape[1] <= 1280):
            raise ValueError('Invalid webcam JPEG or image dimensions')
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        with self.lock:
            now = self.clock()
            if token != self.token or ticket != self.ticket or self.token is None:
                raise RuntimeError('Webcam session/ticket is no longer valid')
            if not 0 <= now - self.ticket_time <= self.max_age_s:
                raise RuntimeError('Webcam capture ticket expired; start a new session')
            if frame is None:
                advanced = sequence > self.sequence and media_time > self.media_time
            else:
                # Some browsers repeat mediaTime for live streams; the presented
                # frame counter proves a new frame, mediaTime must not go back.
                advanced = sequence > self.sequence and frame > self.presented_frame and media_time >= self.media_time
            if not advanced:
                raise RuntimeError(f'Webcam frame did not advance (sequence {sequence}/{self.sequence}, '
                                   f'frame {frame}/{self.presented_frame}, mediaTime {media_time}/{self.media_time})')
            self.sequence, self.media_time = sequence, media_time
            self.frame_sequence = sequence
            self.presented_frame = -1 if frame is None else frame
            self.rgb, self.jpeg = rgb, body
            # Ticket was issued BEFORE capture. Its time is an age upper bound,
            # including browser draw, JPEG encoding, upload and server decoding.
            self.capture_lower_bound = self.ticket_time
            self.valid_until = self.ticket_time + self.max_age_s
            return self._ticket(now)

    def stop(self, token):
        with self.lock:
            if token != self.token or self.token is None:
                return False
            self.client = self.token = self.ticket = self.rgb = self.jpeg = None
            self.valid_until = 0.
            return True

    def frame(self):
        with self.lock:
            now = self.clock()
            age = now - self.capture_lower_bound
            if self.rgb is None or not 0 <= age <= self.max_age_s:
                return None, None, {'available': False, 'source': 'browser_webcam'}
            meta = {'available': True, 'source': 'browser_webcam', 'sequence': self.frame_sequence,
                    'age_upper_bound_s': age, 'width': self.rgb.shape[1], 'height': self.rgb.shape[0],
                    'synchronized_with_wrists': False}
            return self.rgb.copy(), self.jpeg, meta

    def state(self):
        return self.frame()[2]


class BrowserFrontCamera:
    """Read GUMI's browser webcam frames over HTTP."""
    def __init__(self, url='http://127.0.0.1:8620/api/webcam/frame', max_age_s=.8):
        self.url, self.max_age_s, self.closed = url, min(max_age_s, .8), False
        if not self.read()[0]:
            raise RuntimeError('GUMI has no fresh front camera; enable the browser webcam')

    def read(self):
        if self.closed:
            return False, None, None
        started = time.monotonic()
        try:
            response = requests.get(self.url, timeout=.5)
            if response.status_code != 200:
                return False, None, None
            age = float(response.headers['X-Frame-Age-Upper-Bound-S'])
            bgr = cv2.imdecode(np.frombuffer(response.content, dtype=np.uint8), cv2.IMREAD_COLOR)
            age += time.monotonic() - started
            if bgr is None or not math.isfinite(age) or not 0 <= age <= self.max_age_s:
                return False, None, None
            return True, cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), None
        except (requests.RequestException, ValueError, KeyError):
            return False, None, None

    def release(self):
        self.closed = True
