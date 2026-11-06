import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock,patch

import requests
import test_collection as fixtures
from collection import write_json
from gumi_local import handler
from gumi.web_teleop.server import TeleopServer
from training_manager import TrainingManager,parameters


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.CollectionTests();self.fixture.setUp()
        self.path=self.fixture.record();self.identifier=self.path.name
        self.fixture.backend._finish_recording(True)
        self.env=self.fixture.root/'env';self.env.mkdir()
        write_json(self.env/'training-ready.json',{'test':True})
        self.manager=TrainingManager(self.fixture.backend,self.fixture.store,self.fixture.root/'jobs')
        self.fixture.backend.training=self.manager
        self.fixture.backend.datasets=self.fixture.store
        self.env_patch=patch('training_manager.TRAIN_ENV',self.env);self.env_patch.start()
        self.base_patch=patch('training_manager.base_path',return_value=self.fixture.root/'base');self.base_patch.start()

    def tearDown(self):
        self.base_patch.stop();self.env_patch.stop()
        if self.manager.reserved:self.fixture.backend.inference_lock.release()
        self.fixture.tearDown()

    def body(self):return {'episodes':[self.identifier],'profile':'qwen3_5_0_8b','mode':'left_v3','release_inference':True}

    def test_training_parameters_reject_unbounded_or_nonfinite_values(self):
        for body in ({'epochs':float('nan')},{'learning_rate':float('inf')},
                     {'rank':100000},{'max_steps':-8},{'quantization':7},
                     {'profile':'qwen3_5_4b','quantization':0}):
            with self.assertRaises(ValueError):parameters(body)
        self.assertEqual(parameters({})['grasp_repeat'],5)
        for value in (0,21,1.5,True,'5',float('nan')):
            with self.assertRaises(ValueError):parameters({'grasp_repeat':value})
        self.assertEqual(parameters({'grasp_repeat':1})['grasp_repeat'],1)

    def test_start_requires_explicit_gpu_release_and_idle_robot(self):
        with patch('training_manager.subprocess.Popen') as process:
            with self.assertRaises(ValueError):self.manager.start(dict(self.body(),release_inference=False))
            self.fixture.backend.busy=True
            with self.assertRaises(RuntimeError):self.manager.start(self.body())
            process.assert_not_called()

    def test_start_reserves_controls_copies_snapshot_and_disallows_second_job(self):
        with patch('training_manager.subprocess.Popen') as popen,patch('training_manager.threading.Thread'):
            popen.return_value=Mock(pid=12345)
            result=self.manager.start(self.body())
        self.assertTrue(self.manager.reserved)
        self.assertTrue(self.fixture.backend.paused)
        job=self.manager.job_path(result['job']['id'])
        self.assertTrue((job/'episodes'/self.identifier/'actions.jsonl').is_file())
        self.assertFalse(self.fixture.backend._execute('steps',[('MV_FWD','STILL')])['ok'])
        with self.assertRaises(RuntimeError):self.manager.start(self.body())
        with self.assertRaises(ValueError):self.manager.job_path('../README.md')

    def test_archive_does_not_delete_and_can_restore(self):
        self.fixture.store.update(self.identifier,{'archived':True})
        self.assertTrue(self.path.exists())
        self.assertFalse(self.fixture.store.detail(self.identifier)['trainable'])
        self.fixture.store.update(self.identifier,{'archived':False})
        self.assertTrue(self.fixture.store.detail(self.identifier)['trainable'])

    def test_http_catalog_images_and_mutation_origin_guards(self):
        httpd=TeleopServer(('127.0.0.1',0),handler(self.fixture.backend))
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{httpd.server_port}'
        try:
            data=requests.get(url+'/api/training/state').json()
            self.assertEqual(data['episodes'][0]['id'],self.identifier)
            self.assertTrue(data['episodes'][0]['trainable'])
            self.assertIn('LoRA',requests.get(url+'/training').text)
            image=requests.get(url+'/api/training/image',params={'id':self.identifier,'view':'agentview','step':0})
            self.assertEqual(image.status_code,200)
            self.assertEqual(image.headers['Content-Type'],'image/png')
            self.assertEqual(requests.get(url+'/api/training/image',params={'id':'../','view':'agentview'}).status_code,400)
            self.assertEqual(requests.post(url+'/api/training/episode',json={'id':self.identifier,'task':'bad'},headers={'Origin':'http://elsewhere'}).status_code,403)
            good=requests.post(url+'/api/training/episode',json={'id':self.identifier,'task':'updated task'}).json()
            self.assertEqual(good['episode']['task'],'updated task')
            self.assertEqual(requests.post(url+'/api/record/save',json={'success':'true'}).status_code,400)
        finally:
            httpd.shutdown();httpd.server_close();thread.join()


if __name__=='__main__':unittest.main()
