import math
import struct
import unittest
import threading
from unittest.mock import Mock
import test_gripper as fixtures
import numpy as np

class TeleopRange(unittest.TestCase):
    def robot(self):
        r=fixtures.GripperGuards().robot();r.origin=[0]*6
        r.receiver.feedback.limits={i:(-180000,180000,300) for i in range(1,7)}
        return r
    def set_joint(self,r,deg):
        import time
        r.receiver.feedback.accept(0x2A7,struct.pack('>ii',0,round(deg*1000)),time.monotonic())
    def test_motion_beyond_start_envelope_allowed_when_tracking(self):
        r=self.robot();self.set_joint(r,10)
        r.stream_joints(np.radians([0,0,0,0,0,10.1]))
        r.sdk.JointCtrl.assert_called_once_with(0,0,0,0,0,10100)
    def test_manual_waypoint_above_one_degree_is_subdivided(self):
        r=self.robot()
        r.stream_joints(np.radians([0,0,0,0,0,1.201]))
        targets=[call.args[-1] for call in r.sdk.JointCtrl.call_args_list]
        self.assertEqual(len(targets),2)
        self.assertEqual(targets[-1],1201)
        self.assertTrue(all(0<b-a<=1000 for a,b in zip([0]+targets,targets)))
    def test_relaxed_waypoint_below_one_degree_allowed(self):
        r=self.robot()
        r.stream_joints(np.radians([0,0,0,0,0,.8]))
        r.sdk.JointCtrl.assert_called_once_with(0,0,0,0,0,800)
    def test_tracking_lead_refused_even_for_small_waypoint(self):
        r=self.robot();r.last_command=np.radians([0,0,0,0,0,2])
        r.tracking_wait_s=0
        with self.assertRaises(RuntimeError):r.stream_joints(np.radians([0,0,0,0,0,2.1]))
        self.assertEqual(r.sdk.mock_calls,[])
    def test_tracking_wait_sends_only_after_feedback_catches_up(self):
        r=self.robot();r.last_command=np.radians([0,0,0,0,0,2])
        def catch_up(_):
            self.assertEqual(r.sdk.mock_calls,[])
            unlocked=[]
            def probe():
                acquired=r.mutex.acquire(blocking=False);unlocked.append(acquired)
                if acquired:r.mutex.release()
            thread=threading.Thread(target=probe);thread.start();thread.join(1)
            self.assertEqual(unlocked,[True])
            self.set_joint(r,.2)
        r.paused.wait=Mock(side_effect=catch_up)
        r.stream_joints(np.radians([0,0,0,0,0,2.1]))
        r.paused.wait.assert_called_once()
        r.sdk.JointCtrl.assert_called_once_with(0,0,0,0,0,2100)
    def test_pause_interrupts_tracking_wait_without_transmit(self):
        r=self.robot();r.last_command=np.radians([0,0,0,0,0,2])
        r.paused.wait=Mock(side_effect=lambda _:r.paused.set())
        with self.assertRaisesRegex(RuntimeError,'paused'):
            r.stream_joints(np.radians([0,0,0,0,0,2.1]))
        self.assertEqual(r.sdk.mock_calls,[])
    def test_fault_during_tracking_wait_blocks_transmit(self):
        r=self.robot();r.last_command=np.radians([0,0,0,0,0,2])
        def fault(_):r.receiver.feedback.healthy=Mock(side_effect=RuntimeError('driver fault'))
        r.paused.wait=Mock(side_effect=fault)
        with self.assertRaisesRegex(RuntimeError,'driver fault'):
            r.stream_joints(np.radians([0,0,0,0,0,2.1]))
        self.assertEqual(r.sdk.mock_calls,[])
    def test_hardware_limit_refused(self):
        r=self.robot();self.set_joint(r,179.9)
        with self.assertRaises(RuntimeError):r.stream_joints(np.radians([0,0,0,0,0,180.1]))
        self.assertEqual(r.sdk.mock_calls,[])
    def test_model_large_ik_waypoint_is_subdivided_and_tracking_gated(self):
        r=self.robot();r.waypoint_jump_deg=5.
        r.paused.wait=Mock(side_effect=lambda _:self.set_joint(r,2))
        r.stream_joints(np.radians([0,0,0,0,0,3.6]))
        targets=[call.args[-1] for call in r.sdk.JointCtrl.call_args_list]
        self.assertEqual(targets,[900,1800,2700,3600])
        self.assertTrue(all(b-a<=1000 for a,b in zip([0]+targets,targets)))
        r.paused.wait.assert_called_once()
    def test_waypoint_over_five_degrees_tracks_before_sending(self):
        r=self.robot()
        r.paused.wait=Mock(side_effect=lambda _:self.set_joint(r,math.degrees(r.last_command[-1])))
        r.stream_joints(np.radians([0,0,0,0,0,5.1]))
        targets=[call.args[-1] for call in r.sdk.JointCtrl.call_args_list]
        self.assertEqual(targets,[850,1700,2550,3400,4250,5100])
        self.assertGreater(r.paused.wait.call_count,0)
    def test_model_invalid_full_target_rejected_before_subdivision(self):
        r=self.robot();r.waypoint_jump_deg=5.;self.set_joint(r,179)
        with self.assertRaises(RuntimeError):r.stream_joints(np.radians([0,0,0,0,0,181]))
        self.assertEqual(r.sdk.mock_calls,[])
    def test_fifteen_degree_model_waypoint_keeps_small_actuator_targets(self):
        r=self.robot();r.waypoint_jump_deg=15.
        def catch_up(_):self.set_joint(r,math.degrees(r.last_command[-1]))
        r.paused.wait=Mock(side_effect=catch_up)
        r.stream_joints(np.radians([0,0,0,0,0,15]))
        targets=[call.args[-1] for call in r.sdk.JointCtrl.call_args_list]
        self.assertEqual(targets,list(range(1000,15001,1000)))
        self.assertGreater(r.paused.wait.call_count,0)
    def test_waypoint_above_fifteen_keeps_bounded_actuator_targets(self):
        r=self.robot()
        r.paused.wait=Mock(side_effect=lambda _:self.set_joint(r,math.degrees(r.last_command[-1])))
        r.stream_joints(np.radians([0,0,0,0,0,30.1]))
        targets=[call.args[-1] for call in r.sdk.JointCtrl.call_args_list]
        self.assertEqual(len(targets),31)
        self.assertEqual(targets[-1],30100)
        self.assertTrue(all(0<b-a<=1000 for a,b in zip([0]+targets,targets)))
        self.assertGreater(r.paused.wait.call_count,0)
