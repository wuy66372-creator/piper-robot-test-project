"""Model switching must exclude inference and require the selected action LoRA."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from model_switch import ModelSwitch


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.backend = SimpleNamespace(busy=False, lease=SimpleNamespace(current=lambda: None),
            model_run=SimpleNamespace(active=False), inference_lock=threading.Lock(), submit=Mock())
        self.service = ModelSwitch(self.backend)

    def test_registry_must_have_selected_adapter(self):
        with patch('model_switch.registered_models', return_value={'Qwen/Qwen3.5-4B'}):
            with self.assertRaises(RuntimeError): self.service.require('qwen3_5_4b')
        with patch('model_switch.registered_models', return_value={'qwen3_5_4b_showharness_ft'}):
            self.service.require('qwen3_5_4b')

    def test_reject_sim_and_concurrent_load(self):
        with self.assertRaises(ValueError): self.service.start('qwen3_5_2b_sim')
        self.service.info['phase'] = 'loading'
        with self.assertRaises(RuntimeError): self.service.start('qwen3_5_4b')
        with self.assertRaises(RuntimeError): self.service.require('qwen3_5_2b')
        self.backend.submit.assert_not_called()

    def test_validate_before_stop_and_keep_inference_excluded(self):
        calls = []
        def command(*args):
            self.assertFalse(self.backend.inference_lock.acquire(blocking=False))
            calls.append(args)
        self.service._command = command
        with patch.object(self.service, '_registry_or_empty', side_effect=[set(), {'qwen3_5_4b_showharness_ft'}]):
            self.service._load('qwen3_5_4b')
        self.assertEqual(calls, [('serve','qwen3_5_4b','--dry-run'), ('stop',), ('start','qwen3_5_4b')])
        self.backend.submit.assert_called_once_with('hold')
        self.assertEqual(self.service.info['phase'], 'ready')
        self.assertTrue(self.backend.inference_lock.acquire(blocking=False))

    def test_invalid_weights_keep_old_service(self):
        self.service._command = Mock(side_effect=RuntimeError('missing weights'))
        self.service._load('qwen3_5_4b')
        self.service._command.assert_called_once_with('serve', 'qwen3_5_4b', '--dry-run')
        self.assertEqual(self.service.info['phase'], 'error')
        self.assertFalse(self.backend.inference_lock.locked())

    def test_foreign_process_refusal_does_not_start_second_model(self):
        self.service._command = Mock(side_effect=[None, RuntimeError('refusing to signal')])
        with patch.object(self.service, '_registry_or_empty', return_value=set()):
            self.service._load('qwen3_5_4b')
        self.assertEqual(self.service._command.call_count, 2)
        self.assertEqual(self.service.info['phase'], 'error')

    def test_live_policy_selects_each_real_adapter(self):
        from model_control import Policy
        from core.config import load_yaml, resolve_vlm_config
        from pathlib import Path
        settings = load_yaml(Path(__file__).with_name('live_piper.yaml'))
        for profile in ('qwen3_5_0_8b', 'qwen3_5_2b', 'qwen3_5_4b'):
            config = resolve_vlm_config(settings, profile)
            with patch('core.vlm.vlm_client.VLMClient') as client:
                Policy(profile)
                self.assertEqual(client.call_args.kwargs['model'], config['model'])


if __name__ == '__main__': unittest.main()
