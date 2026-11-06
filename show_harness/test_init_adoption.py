import time
import unittest
from unittest.mock import patch
import test_gripper as fixtures
from gumi_local import check_adoption_state,takeover_in_place

class AdoptionGuards(unittest.TestCase):
    def robot(self):
        r=fixtures.GripperGuards().robot()
        f=r.receiver.feedback
        data=bytearray(8);data[0]=2;data[3]=2
        f.accept(0x2A1,data,time.monotonic())
        f.limits={i:(-180000,180000,300) for i in range(1,7)}
        return r
    def refresh(self,r):
        f=r.receiver.feedback
        for cid,(data,_) in list(f.frames.items()):f.accept(cid,data,time.monotonic())
    def test_arbitrary_valid_init_can_be_adopted_without_recovery_window(self):
        r=self.robot();f=r.receiver.feedback
        import struct
        f.accept(0x2A5,struct.pack('>ii',64000,46000),time.monotonic())
        f.accept(0x2A6,struct.pack('>ii',-19000,-11000),time.monotonic())
        f.accept(0x2A7,struct.pack('>ii',41000,-93000),time.monotonic())
        expected=f.joints()
        def mode(ctrl,*args):
            data=bytearray(f.frames[0x2A1][0]);data[0]=ctrl;f.accept(0x2A1,data,time.monotonic())
        r.sdk.MotionCtrl_2.side_effect=mode
        with patch('gumi_local.time.sleep',side_effect=lambda _:self.refresh(r)):
            result=takeover_in_place(r,expected)
        self.assertEqual(result['after']['ctrl_mode'],1)
        self.assertEqual([c.args for c in r.sdk.MotionCtrl_2.call_args_list],[(0,1,0),(1,1,0),(1,1,5)])
        r.sdk.ResetPiper.assert_not_called();r.sdk.EnablePiper.assert_not_called();r.sdk.GripperCtrl.assert_not_called()
        self.assertTrue(all(c.args==tuple(expected) for c in r.sdk.JointCtrl.call_args_list))
    def test_active_recording_and_other_controller_refused(self):
        r=self.robot();f=r.receiver.feedback
        data=bytearray(f.frames[0x2A1][0]);data[3]=1;f.accept(0x2A1,data,time.monotonic())
        with self.assertRaises(RuntimeError):check_adoption_state(f)
        data[3]=2;f.accept(0x2A1,data,time.monotonic());f.accept(0x155,bytes(8),time.monotonic())
        with self.assertRaises(RuntimeError):check_adoption_state(f)
        self.assertEqual(r.sdk.mock_calls,[])
    def test_saved_pose_mismatch_sends_nothing(self):
        r=self.robot()
        with self.assertRaises(RuntimeError):takeover_in_place(r,[300]*6)
        self.assertEqual(r.sdk.mock_calls,[])
    def test_firmware_refusal_never_resets_or_enters_can(self):
        r=self.robot()
        with patch('gumi_local.time.sleep',side_effect=lambda _:self.refresh(r)):
            with self.assertRaises(RuntimeError):takeover_in_place(r,[0]*6)
        r.sdk.MotionCtrl_2.assert_called_once_with(0,1,0)
        r.sdk.JointCtrl.assert_not_called();r.sdk.ResetPiper.assert_not_called();r.sdk.EnablePiper.assert_not_called()
