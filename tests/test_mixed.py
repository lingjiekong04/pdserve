import asyncio
from collections import Counter
import unittest

from pdserve.mixed import FIFOService, Job, ServeError, parse_metrics, validate_request


class FakeLog:
    path = 'logs/baseline/events-test.jsonl'
    def __init__(self): self.rows=[]; self.counts=Counter()
    def emit(self,event,**fields): self.rows.append(dict(event=event,**fields))
    def close(self): pass


class FakeBackend:
    urls={'replica0':'x','replica1':'y'}


class Tests(unittest.TestCase):
    def test_strict_context_and_shape(self):
        valid = {'prompt':'x','max_tokens':1,'stream':True}
        self.assertEqual(validate_request(valid,'safe-1')['output_limit'],1)
        for bad in ({'prompt':['x']},{'prompt':'x','n':2},{'prompt':'x','max_tokens':0}):
            with self.assertRaises(ServeError): validate_request(bad,'safe')

    def test_metrics_parser(self):
        text='sglang:num_running_reqs 2\nsglang:num_used_tokens{model="x"} 17\n# hi\n'
        self.assertEqual(parse_metrics(text),{'running':2.0,'kv_used_tokens':17.0})

    def service(self):
        async def encoder(text): return [1]
        value=FIFOService(FakeBackend(),encoder,FakeLog(),1,4,30)
        value.ready=True
        value.nodes={name:{'token_limit':100,'slot_limit':1,'reserved_tokens':0,
                           'reserved_slots':0,'leases':{}} for name in FakeBackend.urls}
        return value

    def job(self,seq,tokens=10):
        return Job(str(seq),'e'+str(seq),seq,[1]*tokens,5,True,True,0,'short',seq,tokens,
                   buffer=asyncio.Queue())

    def test_round_robin_reservation(self):
        service=self.service(); jobs=[self.job(i) for i in range(2)]
        now=__import__('time').monotonic_ns()
        for job in jobs: job.enqueued_ns=now
        service.queue.extend(jobs)
        service.spawn=lambda coro: (coro.close() or object())
        service.dispatch()
        self.assertEqual([j.replica for j in jobs],['replica0','replica1'])
        self.assertEqual([service.nodes[n]['reserved_slots'] for n in service.names],[1,1])

    def test_fifo_does_not_bypass_head(self):
        service=self.service(); head=self.job(1,96); tail=self.job(2,1)
        now=__import__('time').monotonic_ns(); head.enqueued_ns=tail.enqueued_ns=now
        service.nodes['replica0']['reserved_tokens']=1
        service.nodes['replica1']['reserved_tokens']=1
        service.queue.extend((head,tail)); service.dispatch()
        self.assertEqual(list(service.queue),[head,tail])
        self.assertEqual(tail.state,'queued')

    def test_finish_releases_exact_lease(self):
        service=self.service(); job=self.job(1); job.replica='replica0'; job.state='dispatched'
        node=service.nodes['replica0']; node['leases'][job.engine_id]=15
        node['reserved_tokens']=15; node['reserved_slots']=1
        service.dispatch=lambda:None; service.finish(job)
        self.assertEqual((node['reserved_tokens'],node['reserved_slots']), (0,0))


if __name__=='__main__': unittest.main(verbosity=2)
