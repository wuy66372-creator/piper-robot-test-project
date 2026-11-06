"""Browser-owned, single-arm Show-Harness policy runs on the GUMI robot worker."""
from pathlib import Path
import json
import secrets
import threading
import time

import numpy as np
from model_profiles import config_for,resolve
from dual_model_control import DualRunMixin

ROOT = Path(__file__).resolve().parents[1]
PROFILE = 'qwen3_5_2b'
MODEL = 'qwen3_5_2b_showharness_ft'
PROFILES = ('qwen3_5_0_8b', 'qwen3_5_2b', 'qwen3_5_4b')


def execution_mapping(config):
    """Released Piper adapters use the mixed-corpus depth convention."""
    if config.get('execution_token_swap') != ['MV_FWD', 'MV_BACK']:
        raise ValueError('Released real-arm LoRA requires the configured Piper FWD/BACK mapping')
    left, right = config['execution_token_swap']
    return {left: right, right: left}


class Policy:
    def __init__(self, profile=PROFILE):
        from core.config import load_yaml, resolve_vlm_config
        from core.vlm.vlm_client import VLMClient
        from core.vlm.mvtoken_roles import MvTokenController
        settings = load_yaml(Path(__file__).with_name('live_piper.yaml'))
        config = config_for(settings, profile)
        if config['model'] != profile + '_showharness_ft':
            raise ValueError('Live control requires the released real-arm LoRA')
        execution_mapping(config)
        self.client = VLMClient(**{k: config[k] for k in ('base_url', 'model', 'api_key',
            'max_tokens', 'temperature', 'chat_template_kwargs', 'provider', 'max_retries')}, timeout_s=8)
        self.client.health_check()
        prompt = (ROOT/'Show-Harness/prompts'/settings['prompt']).read_text()
        self.agent = MvTokenController(self.client, prompt)
        self.base_prompt=prompt
        self.use_gripper_state=bool(config.get('gripper_state'))

    def decide(self, task, history, front, wrist, gripper_state=None):
        if self.use_gripper_state:
            from gripper_context import add_context
            if gripper_state is None:raise ValueError('Missing gripper observation')
            self.agent.prompt_template=add_context(self.base_prompt,gripper_state)
        result = self.agent.decide(task, 'unknown', ', '.join(history) or 'none', front, wrist, debug=True)
        if result.raw_text.strip() not in self.agent.allowed_tokens or result.token != result.raw_text.strip():
            raise RuntimeError('Model must emit one exact action token; no fallback motion')
        return {'token': result.token, 'raw_text': result.raw_text,
                'prompt': self.agent.last_prompt, 'media': self.agent.last_media}


class ModelRun(DualRunMixin):
    heartbeat_ttl_s = 2.
    max_decision_age_s = 5.

    def __init__(self, backend, policy_factory=Policy, clock=time.monotonic, output_root=None):
        from core.config import load_yaml
        self.settings = load_yaml(Path(__file__).with_name('live_piper.yaml'))
        self.backend, self.policy_factory, self.clock = backend, policy_factory, clock
        self.output_root = output_root or ROOT/'results/show_harness/live'
        self.lock = threading.RLock()
        self.active = False
        self.profile = PROFILE
        from core.config import resolve_vlm_config
        self.execution_mapping = execution_mapping(resolve_vlm_config(self.settings, PROFILE))
        self.run_id = self.client = None
        self.deadline = 0.
        self.thread = None
        self.history = []
        self.info = {'active': False, 'phase': 'idle', 'message': '单臂模型推理待启动'}

    def state(self):
        with self.lock:
            return dict(self.info, active=self.active, thread_pending=bool(self.thread and self.thread.is_alive()),
                        model=self.profile + '_showharness_ft', profile=self.profile,
                        auto_release_enabled=self.settings['plugins']['auto_release'], step_m=self.settings['fine_step_m'], max_decision_age_s=self.max_decision_age_s,
                        model_waypoint_jump_deg=self.settings['model_waypoint_jump_deg'])

    def valid(self, run_id):
        return self.active and self.run_id == run_id and self.clock() < self.deadline

    def require_valid(self, run_id):
        if not self.valid(run_id):
            raise RuntimeError('Model run stopped or browser heartbeat expired; discarded action')

    def heartbeat(self, body):
        with self.lock:
            self.require_valid(body.get('run_id'))
            if body.get('client') != self.client:
                raise ValueError('Wrong model-run owner')
            self.deadline = self.clock() + self.heartbeat_ttl_s
        return {'ok': True}

    def cancel(self, message=None):
        with self.lock:
            was_active = self.active
            self.active = False
            if was_active or message is not None:
                self.info.update(active=False, phase='stopped', message=message or '模型运行已停止')
            return was_active

    def watchdog(self):
        with self.lock:
            if not self.active:
                return
            reason = None
            if self.clock() >= self.deadline:
                reason = '浏览器心跳中断，模型运行已停止'
            elif not self.backend.session.front.state()['available']:
                reason = '前视相机断流，模型运行已停止'
            if reason:
                self.cancel(reason)
        if reason:
            self.backend.submit('hold')

    def start(self, body):
        # Called on the robot worker, after all earlier manual steps have completed.
        profile = body.get('profile', PROFILE)
        if resolve(profile)['arm']=='dual':return self._start_dual(body)
        from core.config import resolve_vlm_config
        side, task, owner = body.get('arm', 'left'), body.get('task', ''), body.get('client')
        mapping = execution_mapping(config_for(self.settings, profile,side))
        count = body.get('max_steps', 1)
        if side not in ('left', 'right') or not isinstance(task, str) or not 1 <= len(task.strip()) <= 1000:
            raise ValueError('Choose an arm and enter a task of 1..1000 characters')
        if not isinstance(owner, str) or not 8 <= len(owner) <= 80 or type(count) is not int or not 1 <= count <= 150:
            raise ValueError('Invalid browser owner or step limit (1..150)')
        with self.lock:
            if self.active or (self.thread and self.thread.is_alive()):
                raise RuntimeError('Previous inference is still finishing')
            if self.backend.paused or self.backend.busy or self.backend.lease.current():
                raise RuntimeError('Resume control and release manual controls before starting the model')
            if not self.backend.inference_lock.acquire(blocking=False):
                raise RuntimeError('Model preview is still in progress')
            try:
                if hasattr(self.backend, 'model_service'):
                    self.backend.model_service.require(profile)
                obs = self.backend.session.get_observation()
                if not obs['front_camera_available']:
                    raise RuntimeError('Front camera is unavailable or stale')
                controller = self.backend.model_controllers[side]
                controller.sync_from_robot()
                if not controller._joint_stream_ok:
                    raise RuntimeError('Robot FK/IK validation failed')
                for robot in self.backend.session.robots.values():
                    with robot.mutex:
                        robot._feedback().healthy(time.monotonic(), enabled=True)
                        if robot._frame(0x2A1)[0] != 1:
                            raise RuntimeError('Both arms must already be enabled in CAN mode')
                run_id = secrets.token_urlsafe(18)
                destination = self.output_root / (time.strftime('%Y%m%d-%H%M%S') + '-' + run_id[:6])
                destination.mkdir(parents=True, exist_ok=False)
                meta = {'arm': side, 'task': task.strip(), 'model': profile + '_showharness_ft', 'profile': profile, 'max_steps': count,
                        'step_m': self.settings['fine_step_m'], 'motion_frame': self.settings['motion_frame'],
                        'prompt': self.settings['prompt'],
                        'execution_token_swap': mapping, 'automatic_home': False, 'initial_release': False,
                        'gripper_command_scope':'run',
                        'auto_release_enabled': self.settings['plugins']['auto_release'],
                        'auto_release_empty_width_m': (self.settings['empty_width_m']
                            if self.settings['plugins']['auto_release'] else None), 'success': None,
                        'z_floor_m': getattr(controller, 'z_floor_m', None)}
                (destination/'run.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2)+'\n')
                self.profile = profile
                self.execution_mapping = mapping
                self.run_id, self.client, self.history = run_id, owner, []
                self.deadline = self.clock() + self.heartbeat_ttl_s
                self.active = True
                self.info = {'active': True, 'run_id': run_id, 'phase': 'starting', 'arm': side,
                             'task': task.strip(), 'steps': 0, 'max_steps': count,
                             'message': '准备模型推理', 'log_dir': str(destination)}
                self.thread = threading.Thread(target=self._run,
                    args=(run_id, side, task.strip(), count, destination), daemon=True, name='gumi-model')
                self.thread.start()
                return {'ok': True, 'run_id': run_id, 'message': 'Model run started', 'model_run': self.state()}
            except BaseException:
                self.active = False
                self.backend.inference_lock.release()
                raise

    def _run(self, run_id, side, task, count, destination):
        from core.record.images import prepare_view, save_png
        from gripper_context import ScopedCommands
        commands=ScopedCommands()
        try:
            policy = Policy(self.profile) if self.policy_factory is Policy else self.policy_factory()
            for index in range(count):
                with self.lock:
                    self.require_valid(run_id)
                    self.info.update(phase='observing', message='读取前视和腕部图像')
                observed = self.clock()
                obs = self.backend.session.get_observation()
                if not obs['front_camera_available']:
                    raise RuntimeError('No fresh front camera')
                front = prepare_view(obs['agentview'], square_size=self.settings['agentview_square_size'])
                wrist = prepare_view(obs['wrist_'+side], square_size=self.settings['wrist_square_size'])
                save_png(destination/f'{index:03d}-agentview.png', front)
                save_png(destination/f'{index:03d}-wrist.png', wrist)
                with self.lock:
                    self.require_valid(run_id)
                    self.info.update(phase='inferring', message='模型正在推理')
                gripper_state=commands.observe(side,obs[side])
                kwargs={'gripper_state':gripper_state} if getattr(policy,'use_gripper_state',False) else {}
                decision = policy.decide(task, self.history, front, wrist, **kwargs)
                elapsed = self.clock() - observed
                record = dict(decision, step=index, elapsed_s=elapsed, front_camera=obs['front_camera'],
                              observed_monotonic=observed, before_pose=np.asarray(obs[side]['ee_pose']).tolist())
                record['observed_gripper_state']=gripper_state
                (destination/f'{index:03d}-decision.json').write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n')
                with self.lock:
                    self.require_valid(run_id)
                    if elapsed + obs['front_camera']['age_upper_bound_s'] > self.max_decision_age_s:
                        raise RuntimeError('Inference observation is too old; discarded action')
                    # Only execution is mapped; the decision and recent moves stay raw.
                    # Update both fields together so
                    # the UI cannot combine this decision with the previous step's
                    # execution token while the current step is pending or stopped.
                    self.info.update(raw_token=decision['token'],
                                     execution_token=self.execution_mapping.get(decision['token'], decision['token']), elapsed_s=elapsed)
                if decision['token'] == 'DONE':
                    with self.lock:
                        self.active = False
                        self.info.update(phase='done', message='模型输出 DONE；请现场确认任务结果')
                    break
                payload = dict(run_id=run_id, arm=side, token=decision['token'], observed=observed,
                               camera_age=obs['front_camera']['age_upper_bound_s'], before_pose=record['before_pose'])
                result = self.backend.submit('model_step', payload, timeout=30.)
                (destination/f'{index:03d}-execution.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
                if not result.get('ok'):
                    raise RuntimeError(result.get('message', 'Model step failed'))
                commands.completed({side:decision['token']},auto_released=result.get('auto_released',False))
                if (decision['token'].startswith('MV_') and 'reach clamp' in result.get('note', '')
                        and np.linalg.norm(np.asarray(result['after_pose'])[:3]
                                           - np.asarray(result['before_pose'])[:3]) < .001):
                    with self.lock:
                        self.info.update(steps=index+1, execution=result)
                    raise RuntimeError('工作空间限位截断动作，实际位移不足 1 mm；已停止，请检查起始姿态和目标位置')
                if decision['token'].startswith('MV_'):
                    self.history.insert(0, decision['token'])
                    del self.history[5:]
                with self.lock:
                    self.require_valid(run_id)
                    self.info.update(steps=index+1, execution=result, phase='observing')
            else:
                with self.lock:
                    self.active = False
                    self.info.update(phase='complete', message='已达到本次步数上限')
        except Exception as exc:
            with self.lock:
                needs_hold = self.active
                if self.active:
                    self.cancel(str(exc))
                self.info['error'] = str(exc)
            if needs_hold:
                self.backend.submit('hold')
        finally:
            try:
                (destination/'summary.json').write_text(json.dumps(self.state(), ensure_ascii=False, indent=2)+'\n')
            finally:
                self.backend.inference_lock.release()

    def execute(self, payload):
        """Robot worker only. All guards precede any waypoint or gripper command."""
        if payload.get('arm')=='dual':return self._execute_dual(payload)
        side, token = payload['arm'], payload['token']
        if token not in ('MV_FWD', 'MV_BACK', 'MV_LEFT', 'MV_RIGHT', 'MV_UP', 'MV_DOWN', 'GRASP', 'RELEASE'):
            raise ValueError('Invalid live action token')
        execution_token = self.execution_mapping.get(token, token)
        robot = self.backend.session.robots[side]
        controller = self.backend.model_controllers[side]
        with self.lock:
            self.require_valid(payload['run_id'])
            if self.backend.paused or self.clock()-payload['observed']+payload['camera_age'] > self.max_decision_age_s:
                raise RuntimeError('Paused or stale inference; discarded action')
            obs = self.backend.session.get_observation()
            if not obs['front_camera_available']:
                raise RuntimeError('Front camera lost before motion')
            if np.max(np.abs(np.asarray(obs[side]['ee_pose'])-payload['before_pose'])) > .01:
                raise RuntimeError('Arm pose changed while the model was deciding')
            self.backend.busy = True
            robot.paused.clear()
            self.info.update(phase='executing', execution_token=execution_token, message='执行模型动作')
        try:
            robot.motion_deadline=time.monotonic()+20.
            result = controller.step(execution_token)
            auto_released = False
            with self.lock:
                self.require_valid(payload['run_id'])
            if (self.settings['plugins']['auto_release'] and controller.gripper_closed
                    and float(robot.get_gripper_position()[0]) < self.settings['empty_width_m']):
                controller.step('RELEASE')
                auto_released = True
            self.backend.controllers[side].gripper_closed = controller.gripper_closed
            pair = {'left': 'STILL', 'right': 'STILL'}
            pair[side] = execution_token
            self.backend.last_pair = pair
            self.backend.history.append(dict(pair))
            self.backend.pairs_executed += 1
            self.backend._capture()
            return {'ok': True, 'raw_token': token, 'execution_token': pair[side],
                    'auto_released': auto_released, 'before_pose': payload['before_pose'],
                    'after_pose': np.asarray(robot.get_ee_pose()).tolist(), 'note': str(getattr(result, 'note', ''))}
        finally:
            robot.motion_deadline=float('inf')
            self.backend.busy = False
