"""Meaningful admission and task-isolation regressions; no GPUs or sockets."""
import asyncio
from pathlib import Path
import sys
import unittest

from test_gateway import FakeBackend,run_async,ticks
from pdserve.core import Config,ServeError
from pdserve.context_guard import GuardedService


class GuardTests(unittest.TestCase):
    def scenario(self,callback):
        async def run():
            backend=FakeBackend()
            async def encode(prompt):await asyncio.sleep(0);return [1]*len(prompt)
            service=GuardedService(backend,encode,Config(active_limit=2,context=100))
            await service.start(monitor=False)
            try:await callback(service,backend)
            finally:
                service.closing=True
                tasks=list(service.tasks)
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True);await ticks()
        run_async(run())

    def test_equal_boundary_rejected_before_reservation_and_backend(self):
        async def case(s,b):
            with self.assertRaises(ServeError) as caught:await s.submit({'prompt':'x'*80,'max_tokens':20},'equal')
            self.assertEqual(caught.exception.status,413);self.assertTrue(s.ready)
            self.assertFalse(s.jobs);self.assertFalse(s.tokenizing);self.assertEqual(len(s.queue),0)
            self.assertTrue(all(n.used_tokens==0 and n.used_slots==0 for n in s.ledger.nodes.values()))
            self.assertFalse(b.calls)
        self.scenario(case)

    def test_below_boundary_keeps_actual_backend_context_and_dispatches(self):
        async def case(s,b):
            job=await s.submit({'prompt':'x'*79,'max_tokens':20},'below')
            self.assertEqual(s.config.context,100);self.assertEqual(job.prompt_tokens,79)
            self.assertEqual(job.state,'dispatched');self.assertTrue(s.ledger.held(job))
        self.scenario(case)

    def test_above_boundary_rejected(self):
        async def case(s,b):
            with self.assertRaises(ServeError):await s.submit({'prompt':'x'*81,'max_tokens':20},'above')
            self.assertFalse(s.jobs)
        self.scenario(case)

    def test_concurrent_requests_do_not_share_output_limits(self):
        async def case(s,b):
            values=await asyncio.gather(s.submit({'prompt':'x'*80,'max_tokens':19},'allowed'),
                s.submit({'prompt':'x'*80,'max_tokens':20},'rejected'),return_exceptions=True)
            self.assertEqual(values[0].request_id,'allowed');self.assertIsInstance(values[1],ServeError)
            self.assertEqual(set(s.jobs),{'allowed'})
        self.scenario(case)

    def test_strict_rule_is_reported_and_input_context_default_is_not_changed(self):
        async def case(s,b):
            self.assertEqual(s.state()['gateway_admission_patch'],'pdserve-context-strict-v1')
            self.assertEqual(s.config.context,100)
        self.scenario(case)


if __name__=='__main__':unittest.main(verbosity=2)
