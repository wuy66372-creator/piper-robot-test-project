import struct
import unittest
from unittest.mock import Mock
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from piper_control import Feedback, REQUIRED, bounded_target, drive, check_mode_transition, verify_control_device, CAN1_SERIAL
from supported_restore import plan_supported_restore


class Guards(unittest.TestCase):
    def test_right_arm_serial_matches_registered_interface(self):
        with TemporaryDirectory() as tmp:
            serial = Path(tmp) / 'can1/device/serial'
            serial.parent.mkdir(parents=True)
            serial.write_text(CAN1_SERIAL)
            with patch('piper_control.Path', return_value=Path(tmp)):
                verify_control_device('can1')

    def test_swapped_arm_serial_refuses_control(self):
        with TemporaryDirectory() as tmp:
            serial = Path(tmp) / 'can0/device/serial'
            serial.parent.mkdir(parents=True)
            serial.write_text(CAN1_SERIAL)
            with patch('piper_control.Path', return_value=Path(tmp)):
                with self.assertRaises(RuntimeError):
                    verify_control_device('can0')

    def test_supported_recovery_moves_only_outside_margin(self):
        limits = {i: (-70000, 70000, 300) for i in range(1, 7)}
        self.assertEqual(plan_supported_restore([0, 0, 0, 0, 75607, 0], limits),
                         [0, 0, 0, 0, 69500, 0])

    def test_supported_recovery_refuses_large_change(self):
        limits = {i: (-70000, 70000, 300) for i in range(1, 7)}
        with self.assertRaises(RuntimeError):
            plan_supported_restore([0, 0, 0, 0, 80000, 0], limits)

    def test_supported_recovery_refuses_incomplete_limits(self):
        with self.assertRaises(RuntimeError):
            plan_supported_restore([0] * 6, {1: (-70000, 70000, 300)})

    def feedback(self):
        f = Feedback()
        for cid in REQUIRED:
            data = bytes(8)
            if 0x261 <= cid <= 0x266:
                data = bytes([0, 230, 0, 30, 30, 64, 0, 0])
            f.accept(cid, data, 10)
        return f

    def test_one_stale_joint_segment_refuses_control(self):
        f = self.feedback()
        data, _ = f.frames[0x2A7]
        f.frames[0x2A7] = data, 9
        with self.assertRaises(RuntimeError):
            f.healthy(10, enabled=True)

    def test_missing_driver_and_fault_refuse_control(self):
        f = self.feedback()
        del f.frames[0x266]
        with self.assertRaises(RuntimeError):
            f.healthy(10, enabled=True)
        f = self.feedback()
        f.frames[0x261] = bytes([0, 230, 0, 30, 30, 65, 0, 0]), 10
        with self.assertRaises(RuntimeError):
            f.healthy(10, enabled=True)

    def test_disabled_motor_refuses_control(self):
        f = self.feedback()
        f.frames[0x266] = bytes(8), 10
        with self.assertRaises(RuntimeError):
            f.healthy(10, enabled=True)

    def test_limit_responses_are_indexed_without_aliasing(self):
        f = Feedback()
        for i in range(1, 7):
            f.accept(0x473, bytes([i]) + struct.pack('>hhH', i * 100, -i * 100, 300) + b'\0', 10)
        self.assertEqual(f.limits[1], (-10000, 10000, 300))
        self.assertEqual(f.limits[6], (-60000, 60000, 300))
        f.validate_target([0] * 6)
        with self.assertRaises(RuntimeError):
            f.validate_target([10001] + [0] * 5)

    def test_current_out_of_limit_pose_is_refused(self):
        f = self.feedback()
        f.limits = {i: (-1000, 1000, 300) for i in range(1, 7)}
        with self.assertRaises(RuntimeError):
            f.validate_target([0, -1441, 1048, 0, 75454, 0])

    def test_small_target_and_invalid_user_values(self):
        self.assertEqual(bounded_target([0] * 6, 6, 1, 5), [0, 0, 0, 0, 0, 1000])
        for delta, speed in ((3, 5), (float('nan'), 5), (1, 100), (1, 0)):
            with self.assertRaises(ValueError):
                bounded_target([0] * 6, 6, delta, speed)

    def test_unknown_protocol_is_not_standard_feedback(self):
        f = Feedback()
        for cid in range(0x320, 0x328):
            f.accept(cid, bytes(8), 10)
        with self.assertRaises(RuntimeError):
            f.fresh(10)

    def test_no_motion_is_sent_when_feedback_is_stale(self):
        sdk = Mock()
        receiver = Mock()
        receiver.feedback = self.feedback()
        with self.assertRaises(RuntimeError):
            drive(sdk, receiver, [0] * 6, [0] * 6, 1, 5)
        sdk.JointCtrl.assert_not_called()
        sdk.MotionCtrl_2.assert_not_called()

    def test_teaching_requires_recording_end_and_recovery_posture(self):
        f = self.feedback()
        f.frames[0x2A1] = bytes([2, 0, 0, 1, 0, 0, 0, 0]), 10
        with self.assertRaises(RuntimeError):
            check_mode_transition(f)
        f.frames[0x2A1] = bytes([2, 0, 0, 2, 0, 0, 0, 0]), 10
        with self.assertRaises(RuntimeError):
            check_mode_transition(f)  # All zeros is outside the reference J5 window.
        f.frames[0x2A7] = struct.pack('>ii', 30000, -100000), 10
        f.frames[0x2A5] = struct.pack('>ii', 73000, 5000), 10
        f.frames[0x2A6] = struct.pack('>ii', -5000, -71000), 10
        check_mode_transition(f)
        f.frames[0x2A5] = struct.pack('>ii', 73000, 10000), 10
        with self.assertRaises(RuntimeError):
            check_mode_transition(f)
        f.frames[0x2A5] = struct.pack('>ii', 73000, 5000), 10
        f.frames[0x2A7] = struct.pack('>ii', 75000, -100000), 10
        with self.assertRaises(RuntimeError):
            check_mode_transition(f)


if __name__ == '__main__':
    unittest.main()
