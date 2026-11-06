import struct
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
import piper_control as pc
from gumi_local import TeleopRobot,TeleopController

class GripperGuards(unittest.TestCase):
    def robot(self):
        robot=TeleopRobot('left');robot.allow_motion=True
        f=pc.Feedback()
        for cid in pc.REQUIRED:
            data=bytearray(8)
            if cid==0x2A1:data[0]=1
            if 0x261<=cid<=0x266:data[5]=64
            f.accept(cid,data,time.monotonic())
        f.accept(0x2A8,struct.pack('>ihBB',0,0,64,0),time.monotonic())
        robot.receiver=SimpleNamespace(feedback=f);robot.sdk=Mock()
        return robot
    def test_only_gripper_frames_low_effort_no_zero_or_joint_move(self):
        r=self.robot();r.get_gripper_status=Mock(return_value={'width_m':.003,'effort_nm':0.,'fault_bits':0,'enabled':True})
        with patch('gumi_local.time.sleep'):r.set_gripper_position(.003)
        self.assertEqual([c.args for c in r.sdk.GripperCtrl.call_args_list],[(1000,500,1,0),(2000,500,1,0),(3000,500,1,0)])
        r.sdk.JointCtrl.assert_not_called();r.sdk.MotionCtrl_2.assert_not_called()
    def test_bad_width_and_paused_block_transmit(self):
        r=self.robot()
        for width in (float('nan'),float('inf'),-.001,.100001):
            with self.assertRaises(ValueError):r.set_gripper_position(width)
        r.paused.set()
        with self.assertRaises(RuntimeError):r.set_gripper_position(.003)
        self.assertEqual(r.sdk.mock_calls,[])
    def test_fault_blocks_without_clearing(self):
        r=self.robot();r.get_gripper_status=Mock(return_value={'fault_bits':1})
        with self.assertRaises(RuntimeError):r.set_gripper_position(.003)
        self.assertEqual(r.sdk.mock_calls,[])
    def test_close_on_object_never_auto_opens(self):
        r=Mock();r.get_ee_pose.return_value=[0]*7
        controller=TeleopController.__new__(TeleopController);controller.robot=r
        controller.step('GRASP')
        r.set_gripper_position.assert_called_once_with(0.)
        self.assertTrue(controller.gripper_closed)
    def test_release_uses_configured_maximum_on_both_arms(self):
        for side in ('left','right'):
            r=TeleopRobot(side)
            self.assertEqual(r.open_width_m,.1)
            r.get_ee_pose=Mock(return_value=[0]*7)
            r.set_gripper_position=Mock()
            controller=TeleopController.__new__(TeleopController);controller.robot=r
            controller.step('RELEASE')
            r.set_gripper_position.assert_called_once_with(.1)
            self.assertFalse(controller.gripper_closed)
    def test_full_open_ramps_to_100mm_without_joint_or_zero_commands(self):
        r=self.robot()
        r.get_gripper_position=Mock(return_value=[.07])
        r.get_gripper_status=Mock(return_value={'width_m':.1,'effort_nm':0.,'fault_bits':0,'enabled':True})
        with patch('gumi_local.time.sleep'):r.set_gripper_position(.1)
        calls=[c.args for c in r.sdk.GripperCtrl.call_args_list]
        self.assertEqual(calls[-1],(100000,500,1,0))
        self.assertEqual(r.last_gripper_command,'RELEASE')
        self.assertTrue(all(70000<a[0]<=100000 and a[1:]==(500,1,0) for a in calls))
        self.assertTrue(all(0<b-a<=1000 for a,b in zip([70000]+[c[0] for c in calls[:-1]],[c[0] for c in calls])))
        r.sdk.JointCtrl.assert_not_called();r.sdk.MotionCtrl_2.assert_not_called()
    def test_full_open_feedback_and_bounds(self):
        r=self.robot()
        for width in (70000,90000,100000,100200):
            r.receiver.feedback.accept(0x2A8,struct.pack('>ihBB',width,0,64,0),time.monotonic())
            self.assertAlmostEqual(r.get_gripper_position()[0],width/1e6)
        r.receiver.feedback.accept(0x2A8,struct.pack('>ihBB',100201,0,64,0),time.monotonic())
        with self.assertRaises(RuntimeError):r.get_gripper_position()
    def test_command_tracks_success_and_unknown_after_failed_transmission(self):
        r=self.robot()
        r.get_gripper_status=Mock(return_value={'width_m':.04,'effort_nm':.5,'fault_bits':0,'enabled':True})
        with patch('gumi_local.time.sleep'):r.set_gripper_position(0.)
        self.assertEqual(r.last_gripper_command,'GRASP')
        r.sdk.GripperCtrl.side_effect=RuntimeError('send failed')
        with self.assertRaises(RuntimeError):r.set_gripper_position(.1)
        self.assertEqual(r.last_gripper_command,'unknown')
