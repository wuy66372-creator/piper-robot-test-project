import unittest
from continuous import LiveLease

class LeaseGuards(unittest.TestCase):
    def setUp(self):self.now=0.;self.lease=LiveLease(clock=lambda:self.now)
    def test_heartbeat_renews_one_target_without_queue(self):
        self.lease.update('a',1,('MV_UP','STILL'));generation=self.lease.current()[0]
        self.now=.2;self.lease.update('a',2,('MV_UP','STILL'))
        self.assertEqual(self.lease.current()[0],generation)
        self.now=.5;self.assertTrue(self.lease.valid(generation))
        self.now=.61;self.assertTrue(self.lease.expire());self.assertFalse(self.lease.valid(generation))
    def test_delayed_heartbeat_cannot_undo_release(self):
        self.lease.update('a',1,('MV_UP','STILL'));generation=self.lease.current()[0]
        self.lease.update('a',3,None)
        self.lease.update('a',2,('MV_UP','STILL'))
        self.assertIsNone(self.lease.current());self.assertFalse(self.lease.valid(generation))
    def test_direction_change_revokes_old_tick(self):
        self.lease.update('a',1,('MV_UP','STILL'));generation=self.lease.current()[0]
        self.lease.update('a',2,('STILL','MV_LEFT'))
        self.assertFalse(self.lease.valid(generation))
    def test_second_browser_and_bad_tokens_rejected(self):
        self.lease.update('a',1,('MV_UP','STILL'))
        with self.assertRaises(RuntimeError):self.lease.update('b',1,('MV_DOWN','STILL'))
        with self.assertRaises(ValueError):self.lease.update('a',2,('GRASP','STILL'))
        with self.assertRaises(ValueError):self.lease.update('a',2,('nonsense','STILL'))
