import threading
import unittest
from types import SimpleNamespace

import numpy as np
import requests

from realsense_camera import RealSenseFrontStore
from browser_camera import BrowserFrontCamera
from gumi_local import handler
from gumi.web_teleop.server import TeleopServer


class RealSenseFreshness(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.store = RealSenseFrontStore('test-serial', clock=lambda: self.now)
        self.rgb = np.zeros((480,640,3), dtype=np.uint8)
        self.rgb[:,:,0] = 200
        self.depth = np.full((480,640), 1234, dtype=np.uint16)

    def test_fresh_rgb_depth_share_capture_and_stale_frames_disappear(self):
        self.assertTrue(self.store._publish(self.rgb, self.depth, 1, 9.9))
        rgb, jpeg, meta = self.store.frame()
        self.assertTrue(meta['available'])
        self.assertEqual(meta['source'], 'realsense_d435i')
        self.assertAlmostEqual(meta['age_upper_bound_s'], .1)
        self.assertEqual(rgb[0,0,0], 200)
        self.assertEqual(self.store.depth[0,0], 1234)
        self.now = 10.71
        self.assertEqual(self.store.frame()[:2], (None, None))
        self.assertFalse(self.store.state()['available'])

    def test_duplicate_or_queued_stale_frame_cannot_renew_freshness(self):
        self.store._publish(self.rgb, self.depth, 1, 9.9)
        self.now = 10.2
        self.assertFalse(self.store._publish(self.rgb, self.depth, 1, 10.2))
        self.assertFalse(self.store._publish(self.rgb, self.depth, 2, 9.))
        self.assertAlmostEqual(self.store.state()['age_upper_bound_s'], .3)

    def test_browser_cannot_replace_or_stop_host_camera(self):
        self.store._publish(self.rgb, self.depth, 1, 10.)
        with self.assertRaises(RuntimeError): self.store.start('browser-test')
        with self.assertRaises(RuntimeError): self.store.put(b'anything')
        self.assertFalse(self.store.stop(None))
        self.assertTrue(self.store.state()['available'])
        self.store.close()
        self.assertFalse(self.store.state()['available'])

    def test_snapshot_and_model_http_adapter_use_same_host_camera(self):
        store = RealSenseFrontStore()
        store._publish(self.rgb, self.depth, 1, store.clock())
        backend = SimpleNamespace(session=SimpleNamespace(front=store))
        httpd = TeleopServer(('127.0.0.1',0), handler(backend))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{httpd.server_port}'
        try:
            snapshot = requests.get(url+'/snapshot/agentview')
            self.assertEqual(snapshot.status_code, 200)
            self.assertEqual(snapshot.content, store.frame()[1])
            camera = BrowserFrontCamera(url+'/api/webcam/frame')
            self.assertGreater(camera.read()[1][0,0,0],190)
            self.assertEqual(requests.post(url+'/api/webcam/start',json={'client':'browser-test'}).status_code,409)
            store.close()
            self.assertEqual(requests.get(url+'/snapshot/agentview').status_code,503)
            self.assertFalse(camera.read()[0])
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
