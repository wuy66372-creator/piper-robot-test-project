import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import numpy as np
from PIL import Image

from collection import DemonstrationRecorder
from dataset_store import DatasetStore,prepare_training,ROOT
from gumi_local import LocalBackend


class Controller:
    def __init__(self,robot,step):
        self.robot=robot;self.step_m=step;self.gripper_closed=False;self.calls=[]
    def sync_from_robot(self):pass
    def step(self,token):
        self.calls.append(token)
        if token.startswith('MV_'):self.robot.pose[0]+=self.step_m
        if token=='GRASP':self.gripper_closed=True
        if token=='RELEASE':self.gripper_closed=False


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.recorder=DemonstrationRecorder(self.root/'recordings')
        self.robots={}
        for side in ('left','right'):
            feedback=Mock();feedback.frames={0x2A1:(bytes([1,0,0,0,0,0,0,0]),0)}
            robot=SimpleNamespace(paused=threading.Event(),mutex=threading.RLock(),_feedback=Mock(return_value=feedback),
                                  pose=np.array([0.,0.,.3,0.,0.,0.,1.]))
            robot.get_ee_pose=lambda r=robot:r.pose.copy()
            self.robots[side]=robot
        self.front=SimpleNamespace(state=lambda:{'available':True,'source':'test_camera'})
        self.rig=SimpleNamespace(robots=self.robots,front=self.front,get_observation=self.observation)
        self.manual={s:Controller(r,.003) for s,r in self.robots.items()}
        self.backend=LocalBackend(self.rig,self.manual,self.recorder,require_task_done=False,task_text='move the object')
        self.backend.model_controllers={s:Controller(r,.02) for s,r in self.robots.items()}
        self.backend.model_service.state=Mock(return_value={'phase':'ready'})
        self.store=DatasetStore(self.root/'recordings')
        self.store.active_id=lambda:self.recorder.dir.name if self.recorder.active else None
        self.available=True

    def tearDown(self):self.temp.cleanup()

    def observation(self):
        image=np.full((480,640,3),50,dtype=np.uint8)
        obs={'agentview':image,'wrist_left':image,'wrist_right':image,
             'front_camera_available':self.available,'front_camera':{'available':self.available}}
        for s,r in self.robots.items():obs[s]={'ee_pose':r.pose.copy(),'gripper_width':.07}
        return obs

    def record(self):
        self.backend._cmd_start()
        self.assertTrue(self.backend._cmd_steps([('MV_FWD','STILL')])['ok'])
        return self.recorder.dir

    def test_recording_uses_inference_20mm_and_pre_action_observation(self):
        path=self.record()
        row=json.loads((path/'actions.jsonl').read_text())
        self.assertEqual(row['left']['ee_pose'][0],0.)
        self.assertAlmostEqual(self.robots['left'].pose[0],.02)
        self.assertEqual(self.manual['left'].calls,[])
        self.assertEqual(self.backend.model_controllers['left'].calls,['MV_FWD'])
        self.assertEqual(row['execution_status'],'succeeded')
        with Image.open(path/row['agentview']) as image:self.assertEqual(image.size,(256,256))
        with self.assertRaises(RuntimeError):self.backend._cmd_set_task('a different task')

    def test_continuous_recording_repeats_20mm_without_interrupt_on_release(self):
        self.backend._cmd_start()
        with patch.object(self.backend,'_interrupt') as interrupt:
            self.backend.continuous({'client':'test','sequence':1,'left':'MV_FWD'})
            generation,pair=self.backend.lease.current()
            for _ in range(2):
                self.assertTrue(self.backend._execute('live_tick',(generation,pair))['ok'])
            self.backend.continuous({'client':'test','sequence':2})
            interrupt.assert_not_called()
        self.assertFalse(self.backend._execute('live_tick',(generation,pair))['ok'])
        self.assertEqual(len(self.recorder.steps),2)
        self.assertAlmostEqual(self.robots['left'].pose[0],.04)
        self.assertEqual(self.manual['left'].calls,[])
        self.assertTrue(all(s['execution_status']=='succeeded' for s in self.recorder.steps))
        self.backend._finish_recording(True)

    def test_continuous_recording_direction_change_revokes_old_tick(self):
        self.backend._cmd_start()
        self.backend.continuous({'client':'test','sequence':1,'left':'MV_UP'})
        old=self.backend.lease.current()
        self.backend.continuous({'client':'test','sequence':2,'right':'MV_DOWN'})
        self.assertFalse(self.backend._execute('live_tick',old)['ok'])
        self.assertTrue(self.backend._execute('live_tick',self.backend.lease.current())['ok'])
        self.assertEqual(self.backend.model_controllers['left'].calls,[])
        self.assertEqual(self.backend.model_controllers['right'].calls,['MV_DOWN'])
        with self.assertRaises(ValueError):self.backend.continuous({'client':'test','sequence':3,'left':'ROTATE_CW'})

    def test_release_during_step_completes_exactly_one_recorded_20mm(self):
        self.backend._cmd_start()
        self.backend.continuous({'client':'test','sequence':1,'left':'MV_UP'})
        tick=self.backend.lease.current()
        controller=self.backend.model_controllers['left'];original=controller.step
        def release_then_complete(token):
            self.backend.continuous({'client':'test','sequence':2})
            original(token)
        controller.step=release_then_complete
        with patch.object(self.backend,'_interrupt') as interrupt:
            self.assertTrue(self.backend._execute('live_tick',tick)['ok'])
            interrupt.assert_not_called()
        self.assertFalse(self.backend._execute('live_tick',tick)['ok'])
        self.assertEqual(len(self.recorder.steps),1)
        self.assertAlmostEqual(self.robots['left'].pose[0],.02)
        self.assertEqual(self.recorder.steps[0]['execution_status'],'succeeded')

    def test_recording_watchdog_expires_and_requires_explicit_resume(self):
        self.backend._cmd_start()
        self.backend.lease.clock=lambda:0.
        self.backend.continuous({'client':'test','sequence':1,'left':'MV_UP'})
        self.backend.lease.clock=lambda:1.
        with patch.object(self.backend.live_stop,'wait',side_effect=[False,True]), patch.object(self.backend,'_interrupt') as interrupt:
            self.backend._watch_loop()
            interrupt.assert_called_once()
        self.assertTrue(self.backend.paused)
        with self.assertRaises(RuntimeError):self.backend.continuous({'client':'test','sequence':2,'left':'MV_UP'})

    def test_missing_front_rejects_before_motion(self):
        self.backend._cmd_start();self.available=False
        with self.assertRaises(RuntimeError):self.backend._cmd_steps([('MV_FWD','STILL')])
        self.assertEqual(self.backend.model_controllers['left'].calls,[])
        self.assertEqual(len(self.recorder.steps),0)

    def test_interrupted_or_short_motion_is_not_trainable(self):
        self.backend._cmd_start()
        self.backend.model_controllers['left'].step_m=.003
        result=self.backend._cmd_steps([('MV_FWD','STILL')])
        self.assertFalse(result['ok'])
        self.assertEqual(self.recorder.steps[0]['execution_status'],'failed')
        with self.assertRaises(RuntimeError):self.backend._finish_recording(True)
        result=self.backend._finish_recording(False)
        self.assertFalse(self.store.detail(result['episode'])['trainable'])

    def test_success_snapshot_and_v3_converter_align_history_swap_and_done(self):
        path=self.record();identifier=path.name
        self.assertFalse(self.store.detail(identifier)['trainable'])
        self.backend._finish_recording(True)
        self.assertTrue(self.store.detail(identifier)['trainable'])
        snapshot=self.root/'snapshot'
        self.store.snapshot([identifier],snapshot)
        self.store.update(identifier,{'task':'changed after snapshot','archived':True})
        self.assertFalse(self.store.detail(identifier)['trainable'])
        output=self.root/'dataset'
        count=prepare_training(snapshot,'left_v3',output)
        samples=json.loads((output/'samples.json').read_text())
        self.assertEqual(count,2)
        self.assertEqual(samples[0]['output'],'MV_BACK')
        self.assertIn('move the object',samples[0]['instruction'])
        self.assertNotIn('changed after snapshot',samples[0]['instruction'])
        self.assertEqual(samples[-1]['output'],'DONE')
        self.assertTrue(all('/terminal/' in p for p in samples[-1]['images']))
        self.assertIn('MV_BACK',samples[-1]['instruction'])
        self.assertEqual(samples[0]['instruction'].count('<image>'),2)

    def test_dual_converter_keeps_both_labels_and_three_images(self):
        self.backend._cmd_start()
        self.backend._cmd_steps([('MV_FWD','MV_UP')])
        identifier=self.recorder.dir.name
        self.backend._finish_recording(True)
        snapshot=self.root/'snapshot';self.store.snapshot([identifier],snapshot)
        output=self.root/'dataset';prepare_training(snapshot,'dual_v4',output)
        samples=json.loads((output/'samples.json').read_text())
        self.assertEqual(samples[0]['output'],'MV_FWD MV_UP')
        self.assertEqual(samples[0]['instruction'].count('<image>'),3)
        self.assertEqual(samples[-1]['output'],'DONE DONE')
        with self.assertRaises(ValueError):prepare_training(snapshot,'left_v3',self.root/'single')

    def test_store_rejects_paths_active_edits_and_failed_selection(self):
        self.backend._cmd_start();identifier=self.recorder.dir.name
        with self.assertRaises(ValueError):self.store.path('../README.md')
        with self.assertRaises(RuntimeError):self.store.update(identifier,{'task':'overwrite'})
        with self.assertRaises(ValueError):self.store.snapshot([identifier],self.root/'snapshot')
        self.backend._finish_recording(False)
        with self.assertRaises(ValueError):self.store.image(identifier,'../../',0)
        with self.assertRaises(ValueError):self.store.snapshot([identifier],self.root/'snapshot')

    def test_start_requires_enabled_can_and_hardware_valid_pose(self):
        robot=self.robots['left'];robot._feedback.return_value.frames[0x2A1]=(bytes(8),0)
        with self.assertRaisesRegex(RuntimeError,'CAN'):self.backend._cmd_start()
        self.assertFalse(self.recorder.active)

    def test_empty_episode_cannot_be_saved_as_success(self):
        self.backend._cmd_start()
        with self.assertRaises(RuntimeError):self.backend._finish_recording(True)
        self.backend._finish_recording(False)

    def test_explicit_release_executes_even_when_cached_state_is_open(self):
        self.backend.holding['left']=True
        self.manual['left'].gripper_closed=False
        result=self.backend._cmd_steps([('RELEASE','STILL')])
        self.assertTrue(result['ok']);self.assertEqual(result['executed'],1)
        self.assertEqual(self.manual['left'].calls,['RELEASE'])
        self.assertFalse(self.backend.holding['left'])
        self.assertFalse(self.manual['left'].gripper_closed)
        self.assertIs(self.backend.controllers,self.manual)
        self.assertEqual(self.backend._cmd_steps([('RELEASE','STILL')])['executed'],1)
        self.assertEqual(self.manual['left'].calls,['RELEASE','RELEASE'])

    def test_recorded_release_keeps_original_pre_state_and_explicit_label(self):
        self.backend._cmd_start()
        self.assertFalse(self.manual['left'].gripper_closed)
        result=self.backend._cmd_steps([('RELEASE','RELEASE')])
        self.assertTrue(result['ok']);self.assertEqual(result['executed'],1)
        self.assertEqual(self.backend.model_controllers['left'].calls,['RELEASE'])
        self.assertEqual(self.backend.model_controllers['right'].calls,['RELEASE'])
        row=self.recorder.steps[-1]
        self.assertEqual(row['left']['token'],'RELEASE');self.assertEqual(row['right']['token'],'RELEASE')
        self.assertFalse(row['left']['gripper_closed']);self.assertFalse(row['right']['gripper_closed'])
        self.assertEqual(row['execution_status'],'succeeded')
        self.assertFalse(self.manual['left'].gripper_closed)

    def test_explicit_release_still_refuses_pause_and_restores_wrapper_on_failure(self):
        self.backend.paused=True
        self.assertFalse(self.backend._cmd_steps([('RELEASE','STILL')])['ok'])
        self.assertEqual(self.manual['left'].calls,[])
        self.backend.paused=False
        self.manual['left'].step=Mock(side_effect=RuntimeError('gripper fault'))
        result=self.backend._cmd_steps([('RELEASE','STILL')])
        self.assertFalse(result['ok']);self.assertIn('fault',result['message'])
        self.assertIs(self.backend.controllers,self.manual)
        self.assertFalse(self.manual['left'].gripper_closed)

    def test_already_closed_grasp_still_does_not_toggle_or_open(self):
        self.manual['left'].gripper_closed=True
        result=self.backend._cmd_steps([('GRASP','STILL')])
        self.assertEqual(result['executed'],0)
        self.assertEqual(self.manual['left'].calls,[])
        self.assertTrue(self.manual['left'].gripper_closed)


if __name__=='__main__':unittest.main()
