import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np
import requests

from browser_camera import BrowserFrontCamera, BrowserFrontStore
from gumi_local import handler, LocalBackend, NoRecording
from gumi.web_teleop.server import TeleopServer
from core.vlm.vlm_client import VLMResponse


class FrontFreshness(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.store = BrowserFrontStore(clock=lambda: self.now)
        self.session = self.store.start('browser-test')
        image = np.zeros((240,320,3), dtype=np.uint8)
        image[:,:,2] = 200
        self.jpeg = cv2.imencode('.jpg', image)[1].tobytes()

    def put(self, sequence=1, media_time=.1, session=None):
        s = session or self.session
        return self.store.put(self.jpeg, s['token'], s['ticket'], sequence, media_time)

    def test_ticket_bounds_age_including_transit_and_rgb(self):
        self.now += .7
        self.put()
        rgb, _, meta = self.store.frame()
        self.assertGreater(rgb[0,0,0], 190)
        self.assertAlmostEqual(meta['age_upper_bound_s'], .7)
        self.now += .11
        self.assertFalse(self.store.state()['available'])

    def test_expired_upload_does_not_revive_source(self):
        self.now += .81
        with self.assertRaises(RuntimeError): self.put()
        self.assertFalse(self.store.state()['available'])

    def test_same_source_renewal_keeps_original_frame_age_and_revokes_old_ticket(self):
        self.now += .1
        previous = self.put(sequence=5)
        self.now += .2
        renewed = self.store.start('browser-test')
        meta = self.store.state()
        self.assertTrue(meta['available'])
        self.assertEqual(meta['sequence'], 5)
        self.assertAlmostEqual(meta['age_upper_bound_s'], .3)
        with self.assertRaises(RuntimeError): self.put(sequence=6, media_time=.2, session=previous)
        self.now += .51
        self.assertFalse(self.store.state()['available'])
        self.put(sequence=1, media_time=.1, session=renewed)
        self.assertTrue(self.store.state()['available'])
        self.assertAlmostEqual(self.store.state()['age_upper_bound_s'], .51)

    def test_same_source_renewal_never_revives_expired_frame(self):
        self.put()
        self.now += .81
        self.store.start('browser-test')
        self.assertFalse(self.store.state()['available'])

    def test_old_ticket_duplicate_and_nonadvancing_video_rejected(self):
        renewed = self.put()
        with self.assertRaises(RuntimeError): self.put(sequence=2, media_time=.2)
        with self.assertRaises(RuntimeError): self.put(sequence=1, media_time=.2, session=renewed)
        with self.assertRaises(RuntimeError): self.put(sequence=2, media_time=.1, session=renewed)

    def test_presented_frame_counter_allows_repeated_media_time(self):
        s = self.store.put(self.jpeg, self.session['token'], self.session['ticket'], 1, 0., frame=5)
        s = self.store.put(self.jpeg, s['token'], s['ticket'], 2, 0., frame=7)
        with self.assertRaises(RuntimeError): self.store.put(self.jpeg, s['token'], s['ticket'], 3, 0., frame=7)
        with self.assertRaises(RuntimeError): self.store.put(self.jpeg, s['token'], s['ticket'], 3, 0., frame=6)
        self.store.media_time = .5
        with self.assertRaises(RuntimeError): self.store.put(self.jpeg, s['token'], s['ticket'], 3, .4, frame=9)

    def test_stop_revokes_inflight_upload(self):
        self.assertTrue(self.store.stop(self.session['token']))
        with self.assertRaises(RuntimeError): self.put()
        self.assertFalse(self.store.state()['available'])

    def test_one_live_source_and_stale_owner_cannot_stop_new_source(self):
        with self.assertRaises(RuntimeError): self.store.start('another-browser')
        old = self.session
        self.now += .81
        new = self.store.start('another-browser')
        self.assertFalse(self.store.stop(old['token']))
        self.put(session=new)
        self.assertTrue(self.store.state()['available'])

    def test_invalid_image_and_nan_are_rejected(self):
        with self.assertRaises(ValueError): self.put(media_time=float('nan'))
        with self.assertRaises(ValueError):
            self.store.put(b'bad-jpeg', self.session['token'], self.session['ticket'], 1, .1)


class FrontHTTP(unittest.TestCase):
    def setUp(self):
        self.backend = SimpleNamespace(session=SimpleNamespace(front=BrowserFrontStore()),
            frame_cond=threading.Condition(),frames={'agentview':b'placeholder','wrist_left':b'left'})
        self.httpd = TeleopServer(('127.0.0.1',0),handler(self.backend))
        self.thread = threading.Thread(target=self.httpd.serve_forever,daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.httpd.server_port}'

    def tearDown(self):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join()

    def test_missing_front_snapshot_fails_and_client_never_falls_back(self):
        self.assertEqual(requests.get(self.url+'/snapshot/agentview').status_code,503)
        with self.assertRaises(RuntimeError): BrowserFrontCamera(self.url+'/api/webcam/frame')

    def test_page_snapshots_never_show_placeholder_as_front(self):
        self.assertEqual(requests.get(self.url+'/snapshot/wrist_left').content,b'left')
        self.assertEqual(requests.get(self.url+'/snapshot/agentview').status_code,503)
        self.assertEqual(requests.get(self.url+'/snapshot/wrist_right').status_code,404)

    def test_upload_camera_adapter_and_stop(self):
        session = requests.post(self.url+'/api/webcam/start',json={'client':'browser-http'}).json()
        image = cv2.imencode('.jpg',np.zeros((240,320,3),dtype=np.uint8))[1].tobytes()
        headers = {'Content-Type':'image/jpeg','X-Webcam-Token':session['token'],
                   'X-Webcam-Ticket':session['ticket'],'X-Webcam-Sequence':'1','X-Webcam-Media-Time':'.1'}
        self.assertEqual(requests.post(self.url+'/api/webcam/frame',data=image,headers=headers).status_code,200)
        cam = BrowserFrontCamera(self.url+'/api/webcam/frame')
        self.assertEqual(cam.read()[1].shape,(240,320,3))
        requests.post(self.url+'/api/webcam/stop',json={'token':session['token']})
        self.assertFalse(cam.read()[0])
        cam.release()

    def test_wrong_origin_and_oversized_payload_denied(self):
        with requests.Session() as session:
            r = session.post(self.url+'/api/webcam/start',json={'client':'browser-http'},headers={'Origin':'http://elsewhere'})
            self.assertEqual(r.status_code,403)
            self.assertEqual(session.get(self.url+'/').status_code,200)
        r = requests.post(self.url+'/api/webcam/frame',data=b'x'*800001,headers={'Content-Type':'image/jpeg'})
        self.assertEqual(r.status_code,413)

    def test_network_response_age_is_counted(self):
        response = Mock(status_code=200,headers={'X-Frame-Age-Upper-Bound-S':'.7'},
                        content=cv2.imencode('.jpg',np.zeros((240,320,3),dtype=np.uint8))[1].tobytes())
        cam = BrowserFrontCamera.__new__(BrowserFrontCamera)
        cam.url=self.url;cam.max_age_s=.8;cam.closed=False
        with patch('browser_camera.requests.get',return_value=response), patch('browser_camera.time.monotonic',side_effect=[1.,1.2]):
            self.assertFalse(cam.read()[0])


class PreviewIsolation(unittest.TestCase):
    def backend(self, available=True):
        image = np.zeros((240,320,3),dtype=np.uint8)
        controllers = {side:Mock() for side in ('left','right')}
        rig = SimpleNamespace(robots={},get_observation=Mock(return_value={
            'agentview':image,'wrist_left':image,'front_camera_available':available,
            'front_camera':{'available':available,'source':'browser_webcam'}}))
        backend = LocalBackend(rig,controllers,NoRecording(),require_task_done=False)
        backend.model_service.require = Mock()  # camera isolation tests do not contact a live model
        return backend

    def test_preview_does_not_submit_or_step_any_controller(self):
        backend=self.backend()
        backend.submit=Mock(side_effect=AssertionError('Preview must not submit commands'))
        with patch('core.vlm.vlm_client.VLMClient') as Client:
            Client.return_value.complete_action_token.return_value=VLMResponse('MV_UP','MV_UP',{})
            result=backend.model_preview({'task':'pick up a block','arm':'left'})
        self.assertTrue(result['inference_only']);self.assertEqual(result['token'],'MV_UP')
        backend.submit.assert_not_called()
        for controller in backend.controllers.values():controller.step.assert_not_called()

    def test_missing_front_never_sends_model_image_request(self):
        backend=self.backend(available=False)
        with patch('core.vlm.vlm_client.VLMClient') as Client:
            with self.assertRaises(RuntimeError):backend.model_preview({'task':'pick up a block'})
            Client.return_value.complete_action_token.assert_not_called()


if __name__=='__main__': unittest.main()
