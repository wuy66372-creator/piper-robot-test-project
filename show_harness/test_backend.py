import math
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import struct
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import piper_control as pc
from local_backend import LocalPiperInterface, LocalRGBCamera, target_units, install, WristPublisher


class AdapterGuards(unittest.TestCase):
    def test_closed_gripper_small_negative_tolerance_only(self):
        r=self.robot()
        for raw, expected in ((-100, 0.), (-200, 0.), (200, .0002), (100000, .1)):
            with patch.object(r, '_frame', return_value=struct.pack('>i', raw)+bytes(4)):
                self.assertAlmostEqual(r.get_gripper_position()[0], expected)
        for raw in (-201, -10000, 100201):
            with patch.object(r, '_frame', return_value=struct.pack('>i', raw)+bytes(4)):
                with self.assertRaises(RuntimeError):r.get_gripper_position()
        r.sdk.assert_not_called()

    def robot(self):
        robot=LocalPiperInterface('left')
        f=pc.Feedback()
        for cid in pc.REQUIRED:
            data=bytearray(8)
            if cid==0x2A1:data[0]=1
            if 0x261<=cid<=0x266:data[5]=64
            f.accept(cid,data,time.monotonic())
        f.limits={i:(-180000,180000,300) for i in range(1,7)}
        robot.receiver=SimpleNamespace(feedback=f)
        robot.sdk=Mock()
        robot.origin=[0]*6
        return robot

    def test_read_only_sends_no_motion(self):
        r=self.robot()
        r.allow_motion=False
        with self.assertRaises(RuntimeError):r.stream_joints(np.zeros(6))
        self.assertEqual(r.sdk.mock_calls,[])

    def test_nonfinite_and_shape_rejected(self):
        r=self.robot()
        for q in (np.full(6,np.nan),np.full(6,np.inf),np.zeros(7)):
            with self.assertRaises(ValueError):target_units(q,r.origin,r.receiver.feedback)

    def test_cumulative_limit_uses_session_origin(self):
        r=self.robot();r.allow_motion=True
        r.stream_joints(np.radians([0,0,0,0,0,1.9]))
        r.sdk.reset_mock()
        with self.assertRaises(RuntimeError):r.stream_joints(np.radians([0,0,0,0,0,2.1]))
        self.assertEqual(r.sdk.mock_calls,[])

    def test_real_hardware_limit_not_author_limit(self):
        r=self.robot();r.origin[5]=127555
        target=target_units(np.radians([0,0,0,0,0,128.555]),r.origin,r.receiver.feedback)
        self.assertEqual(target[5],128555)
        r.receiver.feedback.limits[6]=(-120000,120000,300)
        with self.assertRaises(RuntimeError):target_units(np.radians([0,0,0,0,0,128.555]),r.origin,r.receiver.feedback)

    def test_bad_goal_rejected_before_partial_move(self):
        r=self.robot();r.allow_motion=True
        with self.assertRaises(RuntimeError):r.move_to_joint_positions(np.radians([20,0,0,0,0,0]),1)
        self.assertEqual(r.sdk.mock_calls,[])

    def test_stale_feedback_blocks_transmit(self):
        r=self.robot();r.allow_motion=True
        data,_=r.receiver.feedback.frames[0x2A1]
        r.receiver.feedback.frames[0x2A1]=(data,time.monotonic()-1)
        with self.assertRaises(RuntimeError):r.stream_joints(np.zeros(6))
        self.assertEqual(r.sdk.mock_calls,[])

    def test_other_controller_blocks_transmit(self):
        r=self.robot();r.allow_motion=True
        r.receiver.feedback.accept(0x155,bytes(8),time.monotonic())
        with self.assertRaises(RuntimeError):r.stream_joints(np.zeros(6))
        self.assertEqual(r.sdk.mock_calls,[])

    def test_teaching_cannot_be_taken_over(self):
        r=self.robot();r.allow_motion=True
        data=bytearray(8);data[0]=2
        r.receiver.feedback.accept(0x2A1,data,time.monotonic())
        with self.assertRaises(RuntimeError):r.stream_joints(np.zeros(6))
        self.assertEqual(r.sdk.mock_calls,[])

    def test_valid_command_is_bounded_low_speed(self):
        r=self.robot();r.allow_motion=True
        r.stream_joints(np.radians([0,0,0,0,0,1]))
        r.sdk.JointCtrl.assert_called_once_with(0,0,0,0,0,1000)
        r.sdk.MotionCtrl_2.assert_called_once_with(1,1,5)

    def test_no_cartesian_gripper_enable_bypass(self):
        r=self.robot();r.allow_motion=True
        for fn,args in ((r.update_desired_ee_pose,[np.zeros(7)]),(r.control_gripper,[True]),
                        (r.enable,[]),(r.disable,[])):
            with self.assertRaises(RuntimeError):fn(*args)
        self.assertEqual(r.sdk.mock_calls,[])

    def test_native_pose_units_and_quaternion(self):
        r=self.robot()
        for cid,values in ((0x2A2,(100000,-200000)),(0x2A3,(300000,0)),(0x2A4,(0,90000))):
            r.receiver.feedback.accept(cid,struct.pack('>ii',*values),time.monotonic())
        pose=r.get_ee_pose()
        np.testing.assert_allclose(pose[:3],[.1,-.2,.3])
        np.testing.assert_allclose(pose[3:],[0,0,math.sqrt(.5),math.sqrt(.5)],atol=1e-10)

    def test_session_binding_and_missing_front(self):
        install()
        import core.piper.piper_session as session
        import core.piper.dual_session as dual
        self.assertIs(session.PiperInterface,LocalPiperInterface)
        self.assertIs(dual.RosImageCamera,LocalRGBCamera)
        with patch('browser_camera.BrowserFrontCamera.read',return_value=(False,None,None)):
            with self.assertRaises(RuntimeError):LocalRGBCamera('/camera_f/color/image_raw')

class CameraFreshness(unittest.TestCase):
    def camera(self,directory):
        cam=LocalRGBCamera.__new__(LocalRGBCamera)
        cam.serial='test';cam.directory=Path(directory);cam.max_age_s=1;cam.closed=False
        return cam

    def test_frame_rgb_and_stale_rejection(self):
        with TemporaryDirectory() as directory:
            cam=self.camera(directory)
            marker=cam.directory/'test.json'
            marker.write_text(json.dumps({'file':'frame.jpg','arrival_monotonic_s':time.monotonic()}))
            with patch.object(WristPublisher,'process',Mock(poll=Mock(return_value=None))), patch('local_backend.cv2.imread',return_value=np.array([[[1,2,3]]],dtype=np.uint8)):
                ok,rgb,depth=cam.read()
                self.assertTrue(ok);self.assertIsNone(depth)
                np.testing.assert_array_equal(rgb,[[[3,2,1]]])
                marker.write_text(json.dumps({'file':'frame.jpg','arrival_monotonic_s':time.monotonic()-2}))
                self.assertFalse(cam.read()[0])

    def test_publisher_exit_invalidates_cached_image(self):
        with TemporaryDirectory() as directory:
            cam=self.camera(directory)
            (cam.directory/'test.json').write_text(json.dumps({'file':'frame.jpg','arrival_monotonic_s':time.monotonic()}))
            with patch.object(WristPublisher,'process',Mock(poll=Mock(return_value=1))):
                self.assertFalse(cam.read()[0])

if __name__=='__main__':unittest.main()
