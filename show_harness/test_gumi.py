import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from gumi_local import LocalBackend,NoRecording,TeleopRobot
from browser_camera import BrowserFrontStore
from gumi.web_teleop_dual.dual_backend import DualTeleopBackend


class GumiGuards(unittest.TestCase):
    def test_read_only_connection_checks_health_without_teaching_takeover(self):
        robot = TeleopRobot('left')
        feedback = Mock()
        with patch('gumi_local.check_adoption_state') as adoption:
            robot.check_connect_mode(feedback)
        feedback.healthy.assert_called_once()
        adoption.assert_not_called()
        self.assertIsNone(robot.sdk)

    def backend(self):
        robots={side:SimpleNamespace(paused=threading.Event(),hold=Mock()) for side in ('left','right')}
        controllers={side:SimpleNamespace(gripper_closed=False,sync_from_robot=Mock()) for side in robots}
        backend=LocalBackend(SimpleNamespace(robots=robots,front=BrowserFrontStore()),controllers,NoRecording(),require_task_done=False)
        backend._capture=Mock()
        return backend

    def test_hold_sets_both_pause_flags_before_holding(self):
        backend=self.backend()
        self.assertTrue(backend.submit('hold')['ok'])
        for robot in backend.session.robots.values():
            self.assertTrue(robot.paused.is_set());robot.hold.assert_called_once()
        self.assertFalse(backend._cmd_steps([('MV_UP','STILL')])['ok'])

    def test_resume_does_not_clear_abort_during_motion(self):
        backend=self.backend();backend.submit('hold');backend.busy=True
        self.assertFalse(backend.submit('resume')['ok'])
        self.assertTrue(all(r.paused.is_set() for r in backend.session.robots.values()))
        backend.busy=False
        self.assertTrue(backend.submit('resume')['ok'])
        self.assertFalse(any(r.paused.is_set() for r in backend.session.robots.values()))

    def test_batch_blocked_before_motion(self):
        backend=self.backend()
        with patch.object(DualTeleopBackend,'_cmd_steps') as execute:
            self.assertFalse(backend._cmd_steps([('MV_UP','STILL')]*2)['ok'])
            execute.assert_not_called()

    def test_single_step_requires_fresh_capture(self):
        backend=self.backend();backend._capture.side_effect=RuntimeError('stale camera')
        with patch.object(DualTeleopBackend,'_cmd_steps') as execute:
            with self.assertRaises(RuntimeError):backend._cmd_steps([('MV_UP','STILL')])
            execute.assert_not_called()

    def test_release_during_pause_keeps_late_heartbeat_revoked(self):
        backend=self.backend()
        backend.continuous({'client':'a','sequence':1,'left':'MV_UP'})
        backend.submit('hold')
        backend.continuous({'client':'a','sequence':3})
        backend.submit('resume')
        backend.continuous({'client':'a','sequence':2,'left':'MV_UP'})
        self.assertIsNone(backend.lease.current())

    def test_no_fake_training_or_cross_base_gap(self):
        backend=self.backend()
        with self.assertRaises(ValueError):backend._cmd_start()
        state=backend.state()
        self.assertIsNone(state['gap_m']);self.assertFalse(state['gap_warn'])
        self.assertTrue(state['recording_available']);self.assertFalse(state['front_camera_available'])

if __name__=='__main__':unittest.main()
