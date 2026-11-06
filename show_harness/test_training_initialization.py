import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import training_initialization as init
from training_manager import parameters

class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=self.root/'store'
        self.source=self.store/'adapters/qwen3_5_0_8b';self.source.mkdir(parents=True)
        self.config=dict(base_model_name_or_path='Qwen/Qwen3.5-0.8B',peft_type='LORA',r=64,
                         target_modules=sorted(init.TARGETS),lora_alpha=128,lora_dropout=.05)
        self.write(self.source/'adapter_config.json',self.config)
        (self.source/'adapter_model.safetensors').write_bytes(b'test fixture')
        (self.root/'show_harness').mkdir();self.write(self.root/'show_harness/models.lock.json',{'adapter_repo':'test/repo','adapter_revision':'fixed'})
        digest=hashlib.sha256(b'test fixture').hexdigest()
        self.write(self.store/'download_receipt.json',{'downloads':[dict(repo='test/repo',revision='fixed',files=[dict(file='qwen3_5_0_8b/adapter_model.safetensors',sha256=digest)])]})
        self.patcher=patch.multiple(init,STORE=self.store,ROOT=self.root);self.patcher.start()
    def write(self,p,obj):p.write_text(json.dumps(obj))
    def tearDown(self):self.patcher.stop();self.temp.cleanup()
    def test_snapshot_and_real_resume_configuration_keep_original_weights(self):
        job=self.root/'job';job.mkdir();meta=init.snapshot('qwen3_5_0_8b',job/'source_adapter')
        self.assertEqual(meta['rank'],64);self.assertEqual(meta['revision'],'fixed')
        config=init.training_config({'initialization':'upstream_lora'},job)
        self.assertFalse(config['create_new_adapter']);self.assertEqual(config['lora_rank'],64)
        self.assertEqual(set(config['lora_target'].split(',')),init.TARGETS)
        self.assertEqual(config['adapter_name_or_path'],str(job/'source_adapter'))
        (job/'source_adapter/adapter_model.safetensors').write_bytes(b'changed snapshot')
        self.assertEqual((self.source/'adapter_model.safetensors').read_bytes(),b'test fixture')
    def test_mismatched_hash_and_base_are_rejected(self):
        (self.source/'adapter_model.safetensors').write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError,'hash'):init.snapshot('qwen3_5_0_8b',self.root/'bad')
        self.config['base_model_name_or_path']='wrong';self.write(self.source/'adapter_config.json',self.config)
        with self.assertRaises(ValueError):init.upstream('qwen3_5_0_8b')
    def test_parameters_reject_wrong_rank_dual_or_unknown_initialization(self):
        with patch('training_manager.base_path'):
            for body in ({'initialization':'upstream_lora','rank':8},
                         {'initialization':'upstream_lora','rank':64,'mode':'dual_v4'},
                         {'initialization':'/arbitrary/path'}):
                with self.assertRaises(ValueError):parameters(body)
            options=parameters({'initialization':'upstream_lora','rank':64})
            self.assertEqual(options['initialization'],'upstream_lora')
            self.assertEqual(parameters({})['initialization'],'base')
    def test_base_initialization_does_not_resume_any_adapter(self):
        self.assertEqual(init.training_config({},self.root),{})

if __name__=='__main__':unittest.main()
