"""Web CAN takeover uses existing transports and never resets/enables/homes."""
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from test_gumi import GumiGuards
from test_init_adoption import AdoptionGuards


class WebTakeover(unittest.TestCase):
    def setUp(self):
        self.backend=GumiGuards().backend()
        self.robot=AdoptionGuards().robot()
        self.backend.session.robots['right']=self.robot
        self.backend.controllers['right']._joint_stream_ok=True
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_selected_arm_current_pose_only_and_remains_paused(self):
        f=self.robot.receiver.feedback
        def mode(value,*_):
            data=bytearray(f.frames[0x2A1][0]);data[0]=value
            f.accept(0x2A1,data,time.monotonic())
        self.robot.sdk.MotionCtrl_2.side_effect=mode
        with patch('gumi_local.ROOT',Path(self.temp.name)), patch('gumi_local.time.sleep',
                side_effect=lambda _:AdoptionGuards().refresh(self.robot)):
            result=self.backend._execute('can_takeover',{'arm':'right'})
        self.assertTrue(result['ok'])
        self.assertTrue(self.backend.paused)
        self.assertTrue(all(r.paused.is_set() for r in self.backend.session.robots.values()))
        self.backend.controllers['right'].sync_from_robot.assert_called_once()
        self.backend.controllers['left'].sync_from_robot.assert_not_called()
        self.robot.sdk.ResetPiper.assert_not_called()
        self.robot.sdk.EnableArm.assert_not_called()
        self.robot.sdk.GripperCtrl.assert_not_called()
        self.assertTrue(list(Path(self.temp.name).rglob('right-web.json')))

    def test_active_model_or_continuous_control_rejected_before_takeover(self):
        self.backend.model_run.active=True
        self.assertFalse(self.backend._cmd_can_takeover({'arm':'right'})['ok'])
        self.backend.model_run.active=False
        self.backend.lease.current=Mock(return_value=(1,('MV_UP','STILL')))
        self.assertFalse(self.backend._cmd_can_takeover({'arm':'right'})['ok'])
        self.assertEqual(self.robot.sdk.mock_calls,[])

    def test_driver_fault_or_unfinished_teaching_rejected_without_send(self):
        f=self.robot.receiver.feedback
        data=bytearray(f.frames[0x2A1][0]);data[3]=1
        f.accept(0x2A1,data,time.monotonic())
        with self.assertRaises(RuntimeError):self.backend._cmd_can_takeover({'arm':'right'})
        data[3]=2;f.accept(0x2A1,data,time.monotonic())
        drive=bytearray(f.frames[0x262][0]);drive[5]=0xb4
        f.accept(0x262,drive,time.monotonic())
        with self.assertRaises(RuntimeError):self.backend._cmd_can_takeover({'arm':'right'})
        self.assertEqual(self.robot.sdk.mock_calls,[])

    def test_invalid_arm_rejected(self):
        with self.assertRaises(ValueError):self.backend._cmd_can_takeover({'arm':'both'})
        self.assertEqual(self.robot.sdk.mock_calls,[])


if __name__=='__main__':unittest.main()
