"""Dual v4 integration with fake robots only; never opens CAN transports."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import numpy as np
import test_collection as collection_fixture
from dual_model_control import DualPolicy,exact_pair

class DualTests(unittest.TestCase):
    setUp=collection_fixture.CollectionTests.setUp
    tearDown=collection_fixture.CollectionTests.tearDown
    def observation(self):
        obs=collection_fixture.CollectionTests.observation(self)
        obs['front_camera']['age_upper_bound_s']=0.
        return obs
    def prepare(self):
        self.run=self.backend.model_run;self.run.profile='test-dual'
        self.run.active=True;self.run.run_id='dual-run';self.run.clock=lambda:0.;self.run.deadline=10.
        self.run.history={'left':[],'right':[]}
        for robot in self.robots.values():robot._frame=lambda _:bytes([1,0,0,0,0,0,0,0]);robot.hold=Mock()
        self.payload=dict(run_id='dual-run',arm='dual',tokens={'left':'MV_FWD','right':'MV_UP'},
            observed=0.,camera_age=0.,before_pose={s:r.pose.copy().tolist() for s,r in self.robots.items()})
    def test_strict_pair_rejects_prose_mixed_done_and_wrong_count(self):
        self.assertEqual(exact_pair('MV_FWD STILL'),{'left':'MV_FWD','right':'STILL'})
        self.assertEqual(exact_pair('DONE DONE'),{'left':'DONE','right':'DONE'})
        for raw in ('Go MV_UP STILL','MV_UP','MV_UP STILL DONE','DONE MV_UP','STILL DONE','MV_UP, STILL','MV_UP  STILL'):
            with self.assertRaises(ValueError):exact_pair(raw)
    def test_both_arms_execute_20mm_without_v3_direction_swap(self):
        self.prepare()
        with patch('dual_model_control.resolve',return_value={'arm':'dual'}):result=self.run.execute(self.payload)
        self.assertTrue(result['ok'])
        for side in ('left','right'):
            self.assertAlmostEqual(self.robots[side].pose[0],.02)
            self.assertEqual(self.manual[side].calls,[])
        self.assertEqual(self.backend.model_controllers['left'].calls,['MV_FWD'])
        self.assertEqual(self.backend.model_controllers['right'].calls,['MV_UP'])
    def test_dual_start_validates_both_arms_without_initial_motion(self):
        self.prepare();self.run.active=False
        for controller in self.backend.model_controllers.values():controller._joint_stream_ok=True
        self.backend.model_service.require=Mock()
        body=dict(profile='test-dual',arm='dual',task='test',client='test-client',max_steps=1)
        self.run.output_root=self.root/'runs'
        with patch('model_control.resolve',return_value={'arm':'dual'}),patch('dual_model_control.resolve',return_value={'arm':'dual'}),patch('dual_model_control.threading.Thread'):
            result=self.run.start(body)
        self.assertTrue(result['ok']);self.assertEqual(result['model_run']['arm'],'dual')
        for controller in self.backend.model_controllers.values():self.assertEqual(controller.calls,[])
        self.backend.inference_lock.release()
    def test_still_never_calls_controller(self):
        self.prepare();self.payload['tokens']['right']='STILL'
        with patch('dual_model_control.resolve',return_value={'arm':'dual'}):self.run.execute(self.payload)
        self.assertEqual(self.backend.model_controllers['right'].calls,[])
    def test_stale_camera_changed_either_pose_and_cancel_reject_both(self):
        self.prepare()
        with patch('dual_model_control.resolve',return_value={'arm':'dual'}):
            self.available=False
            with self.assertRaises(RuntimeError):self.run.execute(self.payload)
            self.available=True;self.robots['right'].pose[0]+=.03
            with self.assertRaises(RuntimeError):self.run.execute(self.payload)
            self.robots['right'].pose[0]=0.;self.payload['camera_age']=6.
            with self.assertRaises(RuntimeError):self.run.execute(self.payload)
            self.payload['camera_age']=0.;self.run.cancel()
            with self.assertRaises(RuntimeError):self.run.execute(self.payload)
        for controller in self.backend.model_controllers.values():self.assertEqual(controller.calls,[])
    def test_one_arm_fault_holds_both_and_rejects_pair(self):
        self.prepare();self.backend.model_controllers['right'].step=Mock(side_effect=RuntimeError('fault'))
        with patch('dual_model_control.resolve',return_value={'arm':'dual'}):
            with self.assertRaisesRegex(RuntimeError,'fault'):self.run.execute(self.payload)
        for robot in self.robots.values():self.assertTrue(robot.paused.is_set());robot.hold.assert_called()
        self.assertFalse(self.backend.busy)
    def test_pair_history_done_and_three_camera_logs(self):
        self.prepare();destination=self.root/'dual';destination.mkdir();self.run.backend.inference_lock.acquire()
        seen=[]
        def decide(task,history,*images):
            seen.append(copy.deepcopy(history));self.assertEqual(len(images),3)
            text='MV_FWD STILL' if len(seen)==1 else 'DONE DONE'
            return dict(tokens=exact_pair(text),token=text,raw_text=text)
        fake=SimpleNamespace(decide=decide)
        self.backend.submit=lambda kind,body=None,**kwargs:self.run.execute(body) if kind=='model_step' else {'ok':True}
        with patch('dual_model_control.resolve',return_value={'arm':'dual'}),patch('dual_model_control.DualPolicy',return_value=fake):
            self.run._run_dual('dual-run','test task',2,destination)
        self.assertEqual(seen,[{'left':[],'right':[]},{'left':['MV_FWD'],'right':[]}])
        self.assertEqual(self.run.info['phase'],'done');self.assertFalse(self.run.active)
        self.assertEqual(self.backend.model_controllers['left'].calls,['MV_FWD'])
        self.assertTrue((destination/'000-wrist_left.png').is_file());self.assertTrue((destination/'000-wrist_right.png').is_file())
        self.assertFalse(self.backend.inference_lock.locked())
    def test_upstream_policy_three_view_order_and_raw_pair(self):
        from core.config import load_yaml
        from pathlib import Path
        settings=load_yaml(Path(__file__).with_name('live_piper.yaml'))
        from core.config import resolve_vlm_config
        config=resolve_vlm_config(settings,'qwen3_5_0_8b')
        front=np.full((256,256,3),1,dtype=np.uint8);left=front+1;right=front+2
        with patch('dual_model_control.config_for',return_value=config),patch('core.vlm.vlm_client.VLMClient') as client:
            client.return_value.complete_action_token_pair.return_value=SimpleNamespace(token='MV_FWD STILL',raw_text='MV_FWD STILL',payload={})
            policy=DualPolicy('test');result=policy.decide('test',{'left':['MV_UP'],'right':[]},front,left,right)
            call=client.return_value.complete_action_token_pair.call_args
            self.assertIs(call.args[2],front);self.assertIs(call.kwargs['wrist_image'][0],left);self.assertIs(call.kwargs['wrist_image'][1],right)
            self.assertIn('newest first: MV_UP',call.args[0]);self.assertEqual(result['tokens']['left'],'MV_FWD')

if __name__=='__main__':unittest.main()
