"""Exercise policy ownership and action guards without opening a hardware driver."""
from collections import deque
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from core.vlm.vlm_client import VLMResponse
from model_control import MODEL, ModelRun, Policy


class FakePolicy:
    def __init__(self, tokens, blocked=False):
        self.tokens = iter(tokens)
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()

    def decide(self, task, history, front, wrist):
        self.calls.append((task, list(history), front.copy(), wrist.copy()))
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError('test did not release fake inference')
        token = next(self.tokens)
        return dict(token=token, raw_text=token, prompt='test prompt', media=[])


class FakeController:
    def __init__(self, robot):
        self.robot = robot
        self._joint_stream_ok = True
        self.gripper_closed = False
        self.sync_from_robot = Mock()
        self.step = Mock(side_effect=self._step)

    def _step(self, token):
        if token in ('GRASP', 'RELEASE'):
            self.gripper_closed = token == 'GRASP'
            self.robot.width = self.robot.close_width if self.gripper_closed else .07
        return SimpleNamespace(note='fake motion only')


class FakeBackend:
    def __init__(self):
        self.paused = self.busy = False
        self.available = True
        self.before_execute = None
        self.inference_lock = threading.Lock()
        self.lease = SimpleNamespace(current=lambda: None)
        self.calls = []
        robots = {}
        for side in ('left', 'right'):
            robot = SimpleNamespace(mutex=threading.RLock(), paused=threading.Event(),
                pose=np.array([.2, .1, .3, 0., 0., 0., 1.]), width=.07, close_width=.02)
            robot._feedback = Mock(return_value=SimpleNamespace(healthy=Mock()))
            robot._frame = Mock(return_value=bytes([1]))
            robot.get_ee_pose = lambda r=robot: r.pose.copy()
            robot.get_gripper_position = lambda r=robot: [r.width]
            robots[side] = robot
        self.session = SimpleNamespace(robots=robots,
            front=SimpleNamespace(state=lambda: {'available': self.available}),
            get_observation=self.observe)
        self.model_controllers = {side: FakeController(robot) for side, robot in robots.items()}
        self.controllers = {side: SimpleNamespace(gripper_closed=False) for side in robots}
        self.history = deque(maxlen=10)
        self.pairs_executed = 0
        self._capture = Mock()

    def observe(self):
        # Different colors expose accidental swapping of agentview and wrist.
        obs = {'agentview': np.full((240, 320, 3), 30, np.uint8),
               'front_camera_available': self.available,
               'front_camera': {'available': self.available, 'age_upper_bound_s': .1}}
        for side, robot in self.session.robots.items():
            obs['wrist_' + side] = np.full((240, 320, 3), 180, np.uint8)
            obs[side] = {'ee_pose': robot.pose.copy(), 'gripper_width': robot.width}
        return obs

    def submit(self, kind, payload=None, timeout=None):
        self.calls.append((kind, payload))
        if kind == 'hold':
            self.run.cancel()
            self.paused = True
            for robot in self.session.robots.values():
                robot.paused.set()
            return {'ok': True}
        if kind == 'model_step':
            if self.before_execute:
                self.before_execute()
            try:
                return self.run.execute(payload)
            except Exception as exc:
                return {'ok': False, 'message': str(exc)}
        raise AssertionError('Unexpected backend call: ' + kind)


class ModelRunGuards(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 10.
        self.backend = FakeBackend()
        self.policy = None
        self.run = None

    def launch(self, tokens=('DONE',), blocked=False, max_steps=None):
        self.policy = FakePolicy(tokens, blocked=blocked)
        self.run = ModelRun(self.backend, policy_factory=lambda: self.policy,
                            clock=lambda: self.now, output_root=Path(self.directory.name))
        self.backend.run = self.run
        self.addCleanup(self.finish)
        result = self.run.start({'client': 'browser-model-test', 'task': 'put block in bowl',
                                 'arm': 'left', 'max_steps': max_steps or len(tokens)})
        self.assertTrue(result['ok'])
        self.assertTrue(self.policy.entered.wait(2), 'fake inference never started')
        return result

    def finish(self):
        if self.policy:
            self.policy.release.set()
        if self.run and self.run.thread:
            self.run.thread.join(3)
            self.assertFalse(self.run.thread.is_alive(), 'model worker did not exit')

    def assert_no_motion(self):
        for controller in self.backend.model_controllers.values():
            controller.step.assert_not_called()
        self.assertEqual(self.backend.pairs_executed, 0)

    def test_done_stops_without_motion_or_success_claim(self):
        self.launch()
        self.finish()
        self.assert_no_motion()
        self.assertEqual(self.run.state()['phase'], 'done')
        self.assertFalse(self.run.state()['active'])
        run_dir = Path(self.run.state()['log_dir'])
        meta = json.loads((run_dir / 'run.json').read_text())
        self.assertIsNone(meta['success'])
        self.assertFalse(meta['initial_release'])
        self.assertFalse(meta['automatic_home'])
        self.assertTrue((run_dir / '000-decision.json').is_file())
        self.assertFalse((run_dir / '000-execution.json').exists())
        self.assertFalse(self.backend.inference_lock.locked())

    def test_completed_run_restart_clears_steps_history_and_last_action(self):
        first=self.launch(('MV_UP',))
        self.finish()
        self.assertEqual(self.run.state()['steps'],1)
        self.assertEqual(self.run.history,['MV_UP'])
        self.policy=FakePolicy(('DONE',),blocked=True)
        self.run.policy_factory=lambda:self.policy
        second=self.run.start({'client':'browser-model-test','task':'second task','arm':'right','max_steps':50})
        self.assertTrue(self.policy.entered.wait(2))
        state=self.run.state()
        self.assertNotEqual(first['run_id'],second['run_id'])
        self.assertEqual(state['steps'],0)
        self.assertEqual(state['max_steps'],50)
        self.assertEqual(self.run.history,[])
        self.assertNotIn('raw_token',state)
        self.assertNotIn('execution',state)

    def test_cancel_discards_late_result_without_stopping_resumed_manual_control(self):
        self.launch(('MV_UP',), blocked=True)
        self.backend.submit('hold')
        self.backend.paused = False  # Operator resumed manual control while VLM finishes.
        for robot in self.backend.session.robots.values():
            robot.paused.clear()
        self.finish()
        self.assert_no_motion()
        self.assertFalse(self.backend.paused, 'late inference interrupted resumed manual control')
        self.assertEqual([kind for kind, _ in self.backend.calls], ['hold'])

    def test_heartbeat_loss_discards_inflight_action(self):
        self.launch(('MV_UP',), blocked=True)
        self.now += 2.01
        self.run.watchdog()
        self.assertFalse(self.run.active)
        self.assertTrue(self.backend.paused)
        self.finish()
        self.assert_no_motion()
        self.assertEqual([kind for kind, _ in self.backend.calls], ['hold'])

    def test_camera_loss_discards_inflight_action(self):
        self.launch(('GRASP',), blocked=True)
        self.backend.available = False
        self.run.watchdog()
        self.finish()
        self.assert_no_motion()
        self.assertTrue(self.backend.paused)

    def test_valid_heartbeat_cannot_revive_cancelled_run_or_change_owner(self):
        result = self.launch(('MV_UP',), blocked=True)
        with self.assertRaises(ValueError):
            self.run.heartbeat({'run_id': result['run_id'], 'client': 'different-owner'})
        self.now += .5
        self.run.heartbeat({'run_id': result['run_id'], 'client': 'browser-model-test'})
        self.assertEqual(self.run.deadline, self.now + self.run.heartbeat_ttl_s)
        self.backend.submit('hold')
        with self.assertRaises(RuntimeError):
            self.run.heartbeat({'run_id': result['run_id'], 'client': 'browser-model-test'})
        self.finish()
        self.assert_no_motion()

    def test_fresh_heartbeat_does_not_make_old_inference_observation_current(self):
        self.launch(('MV_UP',), blocked=True)
        self.now += 6
        self.run.deadline = self.now + 2  # Browser remains present; source observation is old.
        self.finish()
        self.assert_no_motion()
        self.assertIn('too old', self.run.state()['error'])

    def test_camera_rechecked_at_execution_boundary(self):
        self.backend.before_execute = lambda: setattr(self.backend, 'available', False)
        self.launch(('MV_UP',))
        self.finish()
        self.assert_no_motion()
        self.assertIn('camera lost', self.run.state()['error'])

    def test_robot_change_after_inference_prevents_old_action(self):
        def move_robot():
            self.backend.session.robots['left'].pose[0] += .03
        self.backend.before_execute = move_robot
        self.launch(('MV_UP',))
        self.finish()
        self.assert_no_motion()
        self.assertIn('pose changed', self.run.state()['error'])

    def test_raw_history_newest_first_and_swap_only_at_execution(self):
        tokens = ('MV_FWD', 'GRASP', 'MV_BACK', 'RELEASE', 'MV_LEFT', 'MV_RIGHT',
                  'MV_UP', 'MV_DOWN', 'MV_FWD', 'DONE')
        self.launch(tokens)
        self.finish()
        expected_history = []
        for token, call in zip(tokens, self.policy.calls):
            self.assertEqual(call[1], expected_history)
            self.assertEqual(call[2].shape, (256, 256, 3))
            self.assertEqual(call[3].shape, (256, 256, 3))
            self.assertEqual(int(call[2][128, 128, 0]), 30)
            self.assertEqual(int(call[3][128, 128, 0]), 180)
            if token.startswith('MV_'):
                expected_history.insert(0, token)
                expected_history = expected_history[:5]
        self.assertEqual(len(self.policy.calls), len(tokens))
        issued = [call.args[0] for call in self.backend.model_controllers['left'].step.call_args_list]
        self.assertEqual(issued, ['MV_BACK', 'GRASP', 'MV_FWD', 'RELEASE', 'MV_LEFT',
                                 'MV_RIGHT', 'MV_UP', 'MV_DOWN', 'MV_BACK'])
        self.backend.model_controllers['right'].step.assert_not_called()
        self.backend.model_controllers['left'].sync_from_robot.assert_called_once()
        record = json.loads((Path(self.run.state()['log_dir']) / '000-execution.json').read_text())
        self.assertEqual(record['raw_token'], 'MV_FWD')
        self.assertEqual(record['execution_token'], 'MV_BACK')

    def test_reach_clamp_without_progress_stops_instead_of_repeating(self):
        controller = self.backend.model_controllers['left']
        controller.step.side_effect = lambda token: SimpleNamespace(note='reach clamp: workspace boundary')
        self.launch(('MV_BACK', 'MV_BACK', 'DONE'))
        self.finish()
        self.assertEqual(len(self.policy.calls), 1)
        self.assertEqual(controller.step.call_args.args[0], 'MV_FWD')
        self.assertEqual(self.run.state()['steps'], 1)
        self.assertIn('不足 1 mm', self.run.state()['error'])
        self.assertTrue(self.backend.paused)
        self.assertFalse(self.run.active)

    def test_reach_clamp_with_progress_keeps_running(self):
        def step(token):
            self.backend.session.robots['left'].pose[0] += .005
            return SimpleNamespace(note='reach clamp: workspace boundary')
        self.backend.model_controllers['left'].step.side_effect = step
        self.launch(('MV_BACK', 'DONE'))
        self.finish()
        self.assertEqual(len(self.policy.calls), 2)
        self.assertEqual(self.policy.calls[1][1], ['MV_BACK'])
        self.assertEqual(self.run.state()['phase'], 'done')

    def test_thin_object_remains_closed_through_repeated_grasp_and_move(self):
        self.backend.session.robots['left'].close_width = .0002
        self.launch(('GRASP', 'GRASP', 'MV_UP', 'DONE'))
        self.finish()
        issued = [call.args[0] for call in self.backend.model_controllers['left'].step.call_args_list]
        self.assertEqual(issued, ['GRASP', 'GRASP', 'MV_UP'])
        self.assertTrue(self.backend.model_controllers['left'].gripper_closed)
        self.assertTrue(self.backend.controllers['left'].gripper_closed)
        self.assertFalse(self.run.state()['auto_release_enabled'])
        self.assertFalse(self.run.state()['execution']['auto_released'])
        meta = json.loads((Path(self.run.state()['log_dir']) / 'run.json').read_text())
        self.assertIsNone(meta['auto_release_empty_width_m'])
        self.assertIsNone(meta['success'])

    def test_explicit_release_still_opens_after_thin_grasp(self):
        self.backend.session.robots['left'].close_width = .0002
        self.launch(('GRASP', 'RELEASE', 'DONE'))
        self.finish()
        issued = [call.args[0] for call in self.backend.model_controllers['left'].step.call_args_list]
        self.assertEqual(issued, ['GRASP', 'RELEASE'])
        self.assertFalse(self.backend.model_controllers['left'].gripper_closed)

    def test_empty_grasp_auto_releases_after_measured_close(self):
        self.backend.session.robots['left'].close_width = .002
        self.launch(('GRASP',), blocked=True)
        self.run.settings['plugins']['auto_release'] = True
        self.finish()
        issued = [call.args[0] for call in self.backend.model_controllers['left'].step.call_args_list]
        self.assertEqual(issued, ['GRASP', 'RELEASE'])
        self.assertFalse(self.backend.controllers['left'].gripper_closed)
        self.assertTrue(self.run.state()['execution']['auto_released'])

    def test_wrong_profile_rejected_before_observation_or_network(self):
        factory = Mock()
        run = ModelRun(self.backend, policy_factory=factory, output_root=Path(self.directory.name))
        with self.assertRaises(ValueError):
            run.start({'profile': 'qwen3_5_2b_sim'})
        factory.assert_not_called()
        self.assert_no_motion()


class PolicyContract(unittest.TestCase):
    def test_wrong_model_or_swap_rejected_before_contacting_server(self):
        configs = [dict(model='Qwen/Qwen3.5-2B', execution_token_swap=['MV_FWD', 'MV_BACK']),
                   dict(model=MODEL, execution_token_swap=[])]
        for config in configs:
            with self.subTest(config=config), patch('core.config.resolve_vlm_config', return_value=config), \
                    patch('core.vlm.vlm_client.VLMClient') as client:
                with self.assertRaises(ValueError):
                    Policy()
                client.assert_not_called()

    def test_recovered_prose_is_rejected_as_live_motion(self):
        with patch('core.vlm.vlm_client.VLMClient') as client:
            client.return_value.complete_action_token.return_value = VLMResponse('MV_UP', 'Choose MV_UP.', {})
            policy = Policy()
            image = np.zeros((256, 256, 3), np.uint8)
            with self.assertRaises(RuntimeError):
                policy.decide('move up', [], image, image)


if __name__ == '__main__':
    unittest.main()
