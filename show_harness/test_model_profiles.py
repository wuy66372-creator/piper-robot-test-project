import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import model_profiles as profiles
from model_switch import ModelSwitch
from types import SimpleNamespace
import threading

class CustomProfiles(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.jobs=root/'jobs';self.store=root/'models'
        self.identifier='job_20261021_180000_1234abcd'
        self.path=self.jobs/self.identifier;self.adapter=self.path/'adapter';self.adapter.mkdir(parents=True)
        self.job={'status':'completed','options':{'profile':'qwen3_5_0_8b','mode':'right_v3'}}
        self.contract={'mode':'right_v3','step_m':.02,'template':'qwen3_5_nothink','execution_token_swap':['MV_FWD','MV_BACK']}
        self.config={'base_model_name_or_path':str(self.store/'huggingface/hub/Qwen/Qwen3.5-0.8B'),
                     'r':4,'peft_type':'LORA','target_modules':['q_proj','v_proj']}
        self.write(self.path/'job.json',self.job);self.write(self.adapter/'gumi_contract.json',self.contract)
        self.write(self.adapter/'adapter_config.json',self.config)
        # Dummy bytes are only for resolver tests; never sent to a model server.
        (self.adapter/'adapter_model.safetensors').write_bytes(b'test-only')
        self.patch=patch.multiple(profiles,JOBS=self.jobs,STORE=self.store);self.patch.start()
    def write(self,path,data):path.write_text(json.dumps(data))
    def tearDown(self):self.patch.stop();self.temp.cleanup()
    def test_completed_custom_profile_catalog_and_matching_arm(self):
        spec=profiles.resolve(self.identifier,'right')
        self.assertEqual(spec['base'],'qwen3_5_0_8b')
        self.assertEqual(spec['model'],self.identifier+'_showharness_ft')
        self.assertEqual(len(profiles.catalog()),4)
        with self.assertRaises(ValueError):profiles.resolve(self.identifier,'left')
    def test_versioned_gripper_input_contract_and_legacy(self):
        from gripper_context import CONTRACT
        self.assertIsNone(profiles.resolve(self.identifier).get('gripper_state'))
        self.contract['gripper_state']=CONTRACT;self.write(self.adapter/'gumi_contract.json',self.contract)
        self.assertEqual(profiles.resolve(self.identifier)['gripper_state'],CONTRACT)
        with patch('core.config.resolve_vlm_config',return_value={}):
            self.assertEqual(profiles.config_for({},self.identifier)['gripper_state'],CONTRACT)
        self.contract['gripper_state']='unsupported';self.write(self.adapter/'gumi_contract.json',self.contract)
        with self.assertRaises(ValueError):profiles.resolve(self.identifier)
    def test_failed_dual_synthetic_and_wrong_step_are_excluded(self):
        for changes in ({'status':'training'},{'synthetic':True},{'options':{'profile':'qwen3_5_0_8b','mode':'invalid'}}):
            self.write(self.path/'job.json',dict(self.job,**changes));self.assertEqual(len(profiles.catalog()),3)
        self.write(self.path/'job.json',self.job)
        self.contract['step_m']=.003;self.write(self.adapter/'gumi_contract.json',self.contract)
        self.assertEqual(len(profiles.catalog()),3)
    def test_reject_base_rank_path_and_symlink(self):
        for changes in ({'base_model_name_or_path':'wrong/base'},{'r':128},{'target_modules':['all']},{'modules_to_save':['embed_tokens']}):
            self.write(self.adapter/'adapter_config.json',dict(self.config,**changes))
            with self.assertRaises(ValueError):profiles.resolve(self.identifier)
        self.write(self.adapter/'adapter_config.json',self.config)
        with self.assertRaises(ValueError):profiles.resolve('../'+self.identifier)
        weights=self.adapter/'adapter_model.safetensors';weights.unlink();weights.symlink_to(self.path/'job.json')
        with self.assertRaises(ValueError):profiles.resolve(self.identifier)
    def test_custom_require_never_accepts_only_base_registry(self):
        service=ModelSwitch(SimpleNamespace(inference_lock=threading.Lock()))
        with patch('model_switch.registered_models',return_value={'qwen3_5_0_8b_showharness_ft'}):
            with self.assertRaises(RuntimeError):service.require(self.identifier)
        with patch('model_switch.registered_models',return_value={self.identifier+'_showharness_ft'}):
            service.require(self.identifier)
            self.assertEqual(service.state()['profile'],self.identifier)
    def test_custom_policy_config_matches_base_and_overrides_adapter(self):
        with patch('core.config.resolve_vlm_config',return_value={'model':'released','execution_token_swap':['MV_FWD','MV_BACK']}):
            config=profiles.config_for({},self.identifier,'right')
        self.assertEqual(config['model'],self.identifier+'_showharness_ft')

    def test_dual_v4_requires_no_swap_and_dual_arm(self):
        self.job['options']['mode']='dual_v4';self.write(self.path/'job.json',self.job)
        self.contract.update(mode='dual_v4',execution_token_swap=[]);self.write(self.adapter/'gumi_contract.json',self.contract)
        self.assertEqual(profiles.resolve(self.identifier,'dual')['arm'],'dual')
        with self.assertRaises(ValueError):profiles.resolve(self.identifier,'left')
        with patch('core.config.resolve_vlm_config',return_value={'execution_token_swap':['MV_FWD','MV_BACK']}):
            self.assertEqual(profiles.config_for({},self.identifier,'dual')['execution_token_swap'],[])

    def test_continued_upstream_full_target_adapter_is_loadable(self):
        from training_initialization import TARGETS
        self.job['options']['initialization']='upstream_lora';self.job['initialization']={'type':'upstream_lora'}
        self.write(self.path/'job.json',self.job)
        self.config.update(r=64,target_modules=sorted(TARGETS));self.write(self.adapter/'adapter_config.json',self.config)
        self.assertEqual(profiles.resolve(self.identifier,'right')['base'],'qwen3_5_0_8b')

if __name__=='__main__':unittest.main()
