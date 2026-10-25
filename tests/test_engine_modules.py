"""CPU tests of actual added transport/fault modules; no Torch or sockets."""
from types import SimpleNamespace as NS
import unittest
from pdserve_lifecycle import TransportLifecycle
from pdserve_fault_guard import FaultGuard
class Tests(unittest.TestCase):
    def test_retirement_needs_completed_worker(self):
        x=TransportLifecycle('prefill');m=NS(request_status={1:4},transfer_infos={})
        x.submitted(1);self.assertFalse(x.retire(m,1));x.started(1)
        self.assertFalse(x.retire(m,1));x.completed(1,True,True)
        self.assertTrue(x.retire(m,1));self.assertNotIn(1,m.request_status)
    def test_retirement_is_idempotent_and_tombstoned(self):
        x=TransportLifecycle('decode');m=NS(request_status={1:4},transfer_infos={})
        self.assertTrue(x.retire(m,1));self.assertTrue(x.retire(m,1))
        self.assertFalse(x.accepts(1));self.assertEqual(x.late_messages,1)
    def test_transfer_info_blocks_cleanup(self):
        x=TransportLifecycle('decode');m=NS(request_status={1:4},transfer_infos={1:object()})
        self.assertFalse(x.retire(m,1))
    def test_fault_latches_without_freeing(self):
        observer=NS(error=None);x=FaultGuard(observer);x.quarantine('first',1);x.quarantine('later',2)
        self.assertEqual(x.fault['reason'],'first');self.assertEqual(x.fault['room'],1)
        self.assertIn('first',observer.error)
    def test_deadlines_must_be_positive(self):
        for value in (0,-1,float('nan'),float('inf')):
            with self.assertRaises(ValueError):FaultGuard(NS(),value,60)
if __name__=='__main__':unittest.main(verbosity=2)
