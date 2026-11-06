"""Serialize local model reloads with camera inference and robot control."""
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.request import urlopen

from model_service import ROOT, PIDFILE
from model_profiles import resolve,catalog

PROFILES = ('qwen3_5_0_8b', 'qwen3_5_2b', 'qwen3_5_4b')


def registered_models():
    with urlopen('http://127.0.0.1:8000/v1/models', timeout=.5) as response:
        return {item['id'] for item in json.load(response)['data']}


class ModelSwitch:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.RLock()
        self.info = {'phase': 'idle', 'profile': None, 'message': '正在检查后台模型'}
        self.checked = 0.

    def state(self):
        with self.lock:
            if self.info['phase'] != 'loading' and time.monotonic() - self.checked > 1:
                try:
                    ids = registered_models()
                    entries=catalog()
                    # A custom server may also expose its released base adapter.
                    selected=next((p for p in reversed(entries) if p['model'] in ids),None)
                    profile=selected['id'] if selected else None
                    if profile:
                        self.info = {'phase': 'ready', 'profile': profile, 'name':selected['name'], 'message': '模型和 LoRA 已加载'}
                    elif self.info['phase'] != 'error':
                        self.info = {'phase': 'idle', 'profile': None, 'message': '后台未加载支持的模型'}
                except Exception:
                    if self.info['phase'] != 'error':
                        self.info = {'phase': 'idle', 'profile': None, 'message': '后台模型未就绪'}
                self.checked = time.monotonic()
            return dict(self.info)

    def require(self, profile):
        resolve(profile)
        with self.lock:
            if self.info['phase'] == 'loading':
                raise RuntimeError('模型正在切换，请等待加载完成')
            if profile + '_showharness_ft' not in registered_models():
                raise RuntimeError('请先点击“加载所选模型”，等待后台加载完成')

    def start(self, profile):
        resolve(profile)
        with self.lock:
            if self.info['phase'] == 'loading':
                raise RuntimeError('已有模型正在加载')
            if (self.backend.busy and not self.backend.model_run.active) or self.backend.lease.current():
                raise RuntimeError('请先停止手动控制再切换模型')
            self.info = {'phase': 'loading', 'profile': profile, 'message': '暂停控制，等待当前推理结束'}
            threading.Thread(target=self._load, args=(profile,), daemon=True, name='gumi-model-switch').start()
            return {'ok': True, 'model_service': dict(self.info)}

    def _command(self, *args):
        result = subprocess.run(['bash', str(ROOT/'show_harness/models.sh'), *args],
                                capture_output=True, text=True, timeout=35)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip()[-1200:])

    def _load(self, profile):
        acquired = False
        try:
            self.backend.submit('hold')
            acquired = self.backend.inference_lock.acquire(timeout=130)
            if not acquired:
                raise RuntimeError('当前推理未结束，请稍后重试')
            # Validate the installed weights/adapter before stopping a working service.
            self._command('serve', profile, '--dry-run')
            if profile + '_showharness_ft' not in self._registry_or_empty():
                self._message('释放旧模型显存')
                self._command('stop')
                self._message('正在加载基座模型和 LoRA，首次加载可能需要几分钟')
                self._command('start', profile)
                deadline = time.monotonic() + 300
                while profile + '_showharness_ft' not in self._registry_or_empty():
                    if time.monotonic() > deadline:
                        raise RuntimeError('模型加载超时，请检查后台模型日志')
                    if PIDFILE.exists():
                        pid = json.loads(PIDFILE.read_text())['pid']
                        stat = Path(f'/proc/{pid}/stat')
                        if not stat.exists() or stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                            raise RuntimeError('模型进程已退出，请检查后台模型日志')
                    time.sleep(1)
            with self.lock:
                self.info = {'phase': 'ready', 'profile': profile,
                             'message': '模型和 LoRA 已加载；请恢复控制后开始推理'}
                self.checked = time.monotonic()
        except Exception as exc:
            with self.lock:
                self.info = {'phase': 'error', 'profile': profile, 'message': str(exc)}
                self.checked = time.monotonic()
        finally:
            if acquired:
                self.backend.inference_lock.release()

    def _message(self, message):
        with self.lock:
            self.info['message'] = message

    @staticmethod
    def _registry_or_empty():
        try:
            return registered_models()
        except Exception:
            return set()
