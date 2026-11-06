import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np

from gripper_context import CONTRACT, state, add_context, episode_states, ScopedCommands
from dataset_store import prepare_training,resample_grasps
from model_control import Policy
from dual_model_control import DualPolicy
from core.config import load_yaml, resolve_vlm_config
from pathlib import Path
import test_collection as fixtures


class ContextTests(unittest.TestCase):
    def test_scoped_commands_ignore_transport_memory_and_reset(self):
        obs=state(.0994,'RELEASE')
        scope=ScopedCommands()
        self.assertEqual(scope.observe('left',obs),state(.0994))
        scope.completed({'left':'GRASP','right':'STILL'})
        self.assertEqual(scope.observe('left',obs),state(.0994,'GRASP'))
        self.assertEqual(scope.observe('right',obs),state(.0994))
        scope.completed({'left':'MV_UP'})
        self.assertEqual(scope.last['left'],'GRASP')
        scope.completed({'left':'RELEASE'})
        self.assertEqual(scope.last['left'],'RELEASE')
        self.assertEqual(ScopedCommands().observe('left',obs),state(.0994))
        scope.completed({'left':'GRASP'},success=False)
        self.assertEqual(scope.last['left'],'unknown')
    def test_resampling_only_grasp_and_dual_pair_not_squared(self):
        rows=[{'output':t,'instruction':'unchanged','images':['same-image']} for t in
              ('GRASP','RELEASE','MV_UP','DONE','GRASP STILL','STILL GRASP','GRASP GRASP','DONE DONE')]
        before=json.dumps(rows)
        weighted=resample_grasps(rows,5)
        self.assertEqual(len(weighted),24)
        self.assertEqual(json.dumps(rows),before)
        for row in rows:self.assertEqual(weighted.count(row),5 if 'GRASP' in row['output'].split() else 1)
        self.assertEqual(resample_grasps(rows,1),rows)
        for value in (0,21,True,2.5):
            with self.assertRaises(ValueError):resample_grasps(rows,value)
        self.assertEqual(resample_grasps([{'output':'MV_UP'}],5),[{'output':'MV_UP'}])
    def test_validation_and_unknown_are_not_holding_estimates(self):
        for width in (-.001, float('nan'), float('inf'), .101, True):
            with self.assertRaises(ValueError):state(width)
        with self.assertRaises(ValueError):state(.05,'OPEN')
        self.assertEqual(state(),{'gripper_width':None,'last_gripper_command':'unknown'})

    def test_history_is_previous_completed_command_only(self):
        rows=[{s:dict(token=t if s=='left' else 'STILL',gripper_width=.04) for s in ('left','right')}
              for t in ('GRASP','MV_UP','RELEASE','MV_DOWN')]
        before,last=episode_states(rows)
        self.assertEqual([c['left']['last_gripper_command'] for c in before],['unknown','GRASP','GRASP','RELEASE'])
        self.assertEqual(last['right'],'unknown')
        for row in rows:
            for s in ('left','right'):row[s]['last_gripper_command']='RELEASE'
        self.assertEqual(episode_states(rows)[0],before)
        rows[0]['execution_status']='failed'
        self.assertEqual(episode_states(rows)[0][1]['left']['last_gripper_command'],'unknown')


class PipelineTests(unittest.TestCase):
    setUp=fixtures.CollectionTests.setUp
    tearDown=fixtures.CollectionTests.tearDown
    observation=fixtures.CollectionTests.observation

    def test_recording_new_episode_resets_command_and_preserves_width(self):
        original=self.rig.get_observation
        def observe():
            obs=original()
            for s in ('left','right'):obs[s]['last_gripper_command']='RELEASE'
            return obs
        self.rig.get_observation=observe
        for token in ('GRASP','RELEASE'):
            self.backend._cmd_start()
            path=self.recorder.dir
            self.assertTrue(self.backend._cmd_steps([(token,'STILL')])['ok'])
            self.backend._finish_recording(True)
            row=json.loads((path/'actions.jsonl').read_text())
            self.assertEqual(row['left']['last_gripper_command'],'unknown')
            self.assertEqual(row['left']['gripper_width'],.07)
            terminal=json.loads((path/'metadata.json').read_text())['terminal_gripper_state']
            self.assertEqual(terminal['left'],state(.07,token))
            self.assertEqual(terminal['right'],state(.07))

    def test_actual_single_run_restarts_with_unknown_after_release(self):
        from test_model_control import FakeBackend
        from model_control import ModelRun
        backend=FakeBackend();seen=[];tokens=iter(('GRASP','RELEASE','DONE','DONE'))
        original=backend.observe
        def observe():
            obs=original()
            for s in ('left','right'):obs[s]['last_gripper_command']='RELEASE'
            return obs
        backend.session.get_observation=observe
        def decide(task,history,front,wrist,gripper_state=None):
            seen.append(dict(gripper_state));token=next(tokens)
            return dict(token=token,raw_text=token,prompt='test')
        policy=SimpleNamespace(use_gripper_state=True,decide=decide)
        run=ModelRun(backend,policy_factory=lambda:policy,clock=lambda:0.,output_root=self.root/'single-runs')
        backend.run=run
        for count in (3,1):
            run.start(dict(client='test-browser-owner',task='test',arm='left',max_steps=count))
            run.thread.join(3)
            self.assertFalse(run.thread.is_alive())
            self.assertNotIn('error',run.info)
        self.assertEqual([s['last_gripper_command'] for s in seen],['unknown','GRASP','RELEASE','unknown'])
        self.assertEqual(seen[-1]['gripper_width'],.07)

    def test_actual_dual_run_resets_both_sides_independently(self):
        from dual_model_control import exact_pair
        run=self.backend.model_run;seen=[];tokens=iter(('GRASP STILL','RELEASE GRASP','DONE DONE','DONE DONE'))
        original=self.rig.get_observation
        def observe():
            obs=original();obs['front_camera']['age_upper_bound_s']=0.
            for s in ('left','right'):obs[s]['last_gripper_command']='RELEASE'
            return obs
        self.rig.get_observation=observe
        def decide(task,history,*images,gripper_state=None):
            seen.append({s:dict(v) for s,v in gripper_state.items()});raw=next(tokens)
            return dict(token=raw,raw_text=raw,tokens=exact_pair(raw))
        policy=SimpleNamespace(use_gripper_state=True,decide=decide)
        self.backend.submit=lambda *a,**kw:dict(ok=True,after_pose={s:r.pose.tolist() for s,r in self.robots.items()})
        with patch('dual_model_control.DualPolicy',return_value=policy):
            for i,count in enumerate((3,1)):
                run.active=True;run.run_id='test-run';run.deadline=10.;run.clock=lambda:0.
                run.history={'left':[],'right':[]};self.backend.inference_lock.acquire()
                destination=self.root/f'dual-{i}';destination.mkdir()
                run._run_dual('test-run','test',count,destination)
                self.assertNotIn('error',run.info)
        self.assertEqual([[s[a]['last_gripper_command'] for a in ('left','right')] for s in seen],
                         [['unknown','unknown'],['GRASP','unknown'],['RELEASE','GRASP'],['unknown','unknown']])
        self.assertEqual(seen[-1]['left']['gripper_width'],.07)

    def dataset(self,mode,legacy=False,grasp_repeat=1):
        path=fixtures.CollectionTests.record(self)
        for token in ('GRASP','MV_UP','RELEASE'):
            self.assertTrue(self.backend._cmd_steps([(token,'STILL')])['ok'])
        self.backend._finish_recording(True)
        rows=[json.loads(x) for x in (path/'actions.jsonl').read_text().splitlines()]
        for i,row in enumerate(rows):
            for s in ('left','right'):row[s].pop('last_gripper_command',None)
            row['left']['gripper_width']=[.1,.1,.04,.04][i]
            if mode=='right_v3':row['left'],row['right']=row['right'],row['left']
        (path/'actions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        meta=json.loads((path/'metadata.json').read_text())
        if legacy:meta.pop('terminal_gripper_state',None)
        else:meta['terminal_gripper_state']={s:state(.1,'RELEASE') for s in ('left','right')}
        (path/'metadata.json').write_text(json.dumps(meta))
        snapshot=self.root/'snapshot';self.store.snapshot([path.name],snapshot)
        output=self.root/'dataset';prepare_training(snapshot,mode,output,grasp_repeat=grasp_repeat)
        return json.loads((output/'samples.json').read_text())

    def test_sampling_stats_and_original_state_history_preserved(self):
        samples=self.dataset('left_v3',grasp_repeat=5)
        original=json.loads((self.root/'dataset/samples_unweighted.json').read_text())
        stats=json.loads((self.root/'dataset/sampling.json').read_text())
        self.assertEqual(len(original),5);self.assertEqual(len(samples),9)
        self.assertEqual(stats['original_grasp_samples'],1)
        self.assertEqual(stats['training_grasp_samples'],5)
        self.assertEqual(stats['training_grasp_fraction'],5/9)
        self.assertEqual([s for s in samples if s['output']=='GRASP'],[original[1]]*5)
        self.assertIn('Gripper last command: unknown',original[1]['instruction'])
        self.assertIn('Gripper last command: GRASP',original[2]['instruction'])

    def policy(self,dual,version=CONTRACT):
        config=resolve_vlm_config(load_yaml(Path(__file__).with_name('live_piper.yaml')),'qwen3_5_0_8b')
        config['gripper_state']=version
        config['model']='test_showharness_ft'
        if dual:config['execution_token_swap']=[]
        return config

    def test_single_training_matches_actual_inference_wire_prompt(self):
        samples=self.dataset('left_v3')
        self.assertIn('Gripper measured width (mm): 40.000',samples[2]['instruction'])
        self.assertIn('Gripper last command: unknown',samples[1]['instruction'])
        self.assertIn('Gripper last command: GRASP',samples[2]['instruction'])
        self.assertIn('Gripper last command: RELEASE',samples[-1]['instruction'])
        with patch('model_control.config_for',return_value=self.policy(False)),patch('core.vlm.vlm_client.VLMClient') as client:
            client.return_value.complete_action_token.return_value=SimpleNamespace(token='MV_UP',raw_text='MV_UP')
            policy=Policy('test');image=np.zeros((256,256,3),dtype=np.uint8)
            policy.decide('move the object',['MV_BACK'],image,image,gripper_state=state(.04,'GRASP'))
            wire=client.return_value.complete_action_token.call_args.args[0]
            self.assertEqual(samples[2]['instruction'],'<image><image>'+wire)
            policy.decide('move the object',[],image,image,gripper_state=state(.1,'RELEASE'))
            self.assertEqual(policy.agent.last_prompt.count('Gripper measured width'),1)
            with self.assertRaises(ValueError):policy.decide('test',[],image,image)

    def test_right_arm_and_legacy_terminal_unknown(self):
        samples=self.dataset('right_v3',legacy=True)
        self.assertIn('Gripper last command: GRASP',samples[2]['instruction'])
        self.assertIn('Gripper measured width (mm): unknown',samples[-1]['instruction'])
        self.assertIn('Gripper last command: RELEASE',samples[-1]['instruction'])

    def test_dual_training_matches_actual_inference_wire_prompt(self):
        samples=self.dataset('dual_v4')
        with patch('dual_model_control.config_for',return_value=self.policy(True)),patch('core.vlm.vlm_client.VLMClient') as client:
            client.return_value.complete_action_token_pair.return_value=SimpleNamespace(token='MV_UP STILL',raw_text='MV_UP STILL',payload={})
            policy=DualPolicy('test');image=np.zeros((256,256,3),dtype=np.uint8)
            # Upstream dual history includes STILL for the waiting arm.
            policy.decide('move the object',{'left':['MV_FWD'],'right':['STILL','STILL']},image,image,image,
                          gripper_state={'left':state(.04,'GRASP'),'right':state(.07)})
            wire=client.return_value.complete_action_token_pair.call_args.args[0]
            self.assertEqual(samples[2]['instruction'],'<image><image><image>'+wire)

    def test_legacy_policy_keeps_original_prompt(self):
        for dual in (False,True):
            module='dual_model_control' if dual else 'model_control'
            with patch(module+'.config_for',return_value=self.policy(dual,None)),patch('core.vlm.vlm_client.VLMClient'):
                policy=DualPolicy('test') if dual else Policy('test')
                self.assertFalse(policy.use_gripper_state)
                self.assertNotIn('measured width',policy.agent.prompt_template)


if __name__=='__main__':unittest.main()
