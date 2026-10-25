"""CPU-only deterministic budget, queue, protocol and lifecycle tests."""
import asyncio
import copy
import gc
import json
import os
from types import SimpleNamespace
import time
import unittest
from pdserve.core import Config, Ledger, PATCH_ID, ROLES, ServeError, WaitQueue
from pdserve.protocol import SSEParser, completion_options
from pdserve.service import Service


def state(role, tokens=10000, page=16):
    return dict(patch_id=PATCH_ID, role=role, epoch=role+'-epoch1', enabled=True, healthy=True,
        cleanup_confirmation_supported=True, cancellation_supported=True,
        fault_guard=dict(enabled=True, quarantined=False, test_hooks_enabled=False),
        capacity=dict(kv_tokens=tokens,page_size=page,kv_bytes_per_token=57344,request_slots=10,metadata_slots=10),
        resources=dict(kv_free_tokens=tokens,kv_used_tokens=0,kv_evictable_tokens=0,
            request_free_slots=10,metadata_free_slots=10,queues=dict(running=0,waiting=0),
            transport_status_entries=0,transport_info_entries=0,transport_queued_chunks=0),
        pending_cancels=0,transport_lifecycle=dict(active_chunks=0,pending_chunks=0),
        events_oldest_seq=1,events_latest_seq=0,events=[],requests=[])


def sample(sequence=1,category='short',age=0,prompt=17,output=17):
    return SimpleNamespace(engine_id=f'r{sequence}', room=sequence, input_ids=[1]*prompt,output_limit=output,
                           sequence=sequence,category=category,enqueued_at=time.monotonic()-age)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.config=Config()
        self.ledger=Ledger({role:state(role) for role in ROLES},self.config)

    def test_page_rounding_margin_and_slots(self):
        self.assertEqual(self.ledger.demand(17,17),dict(prefill=32,decode=48))
        self.assertEqual(self.ledger.nodes['decode'].token_limit,8992)
        self.assertEqual(self.ledger.nodes['decode'].slot_limit,9)

    def test_atomic_and_duplicate_reserve(self):
        job=sample();self.ledger.reserve(job)
        with self.assertRaises(ValueError):self.ledger.reserve(job)
        self.assertEqual(self.ledger.nodes['prefill'].used_tokens,32)
        huge=sample(2,prompt=9000)
        with self.assertRaises(ValueError):self.ledger.reserve(huge)
        self.assertEqual(self.ledger.nodes['prefill'].used_slots,1)

    def test_ack_identity_and_idempotence(self):
        job=sample();self.ledger.reserve(job)
        record=dict(rid=job.engine_id,bootstrap_room=job.room,cleanup_confirmed=True,compute_pending=0,
                    allocator_release_seen=True,metadata_release_seen=True,cleanup_scope='normal_success')
        for key,value in [('bootstrap_room',99),('compute_pending',1),('metadata_release_seen',False)]:
            with self.assertRaises(ValueError):self.ledger.release_ack('prefill',job,'prefill-epoch1',{**record,key:value})
        with self.assertRaises(ValueError):self.ledger.release_ack('prefill',job,'old',record)
        self.assertTrue(self.ledger.release_ack('prefill',job,'prefill-epoch1',record))
        self.assertFalse(self.ledger.release_ack('prefill',job,'prefill-epoch1',record))
        self.assertTrue(self.ledger.held(job))

    def test_fair_three_to_one_and_fifo(self):
        queue=WaitQueue(self.config)
        for i in range(1,9):queue.add(sample(i,'short' if i<=6 else 'long'))
        selected=[]
        for _ in range(8):
            job=queue.choose(self.ledger,time.monotonic());selected.append(job.sequence);queue.remove(job)
        self.assertEqual(selected,[1,2,3,7,4,5,6,8])
        queue=WaitQueue(Config(mode='fifo'));queue.add(sample(2));queue.add(sample(1,'long'))
        self.assertEqual(queue.choose(self.ledger,time.monotonic()).sequence,1)

    def test_long_age_and_same_class_head_block(self):
        queue=WaitQueue(self.config);queue.add(sample(1));queue.add(sample(2,'long',age=6))
        self.assertEqual(queue.choose(self.ledger,time.monotonic()).sequence,2)
        queue=WaitQueue(self.config);queue.add(sample(1,prompt=9999));queue.add(sample(2))
        self.assertIsNone(queue.choose(self.ledger,time.monotonic()))

    def test_overflow_timeout_capacity(self):
        queue=WaitQueue(Config(queue_limit=1));job=sample(age=31);queue.add(job)
        with self.assertRaises(ServeError) as result:queue.add(sample(2))
        self.assertEqual(result.exception.status,429)
        self.assertEqual(queue.expired(time.monotonic()),[job])
        with self.assertRaises(ServeError):self.ledger.check_single(sample(prompt=8992,output=1))


class ProtocolTests(unittest.TestCase):
    def test_reject_unsupported_input(self):
        for patch in [dict(prompt=['x']),dict(n=2),dict(max_tokens=True),dict(stream=1),dict(temperature=float('nan')),dict(tools=[])]:
            with self.assertRaises(ServeError):completion_options({'prompt':'x',**patch},'m','id')
        with self.assertRaises(ServeError):completion_options({'prompt':'x'},'m','bad id')

    def test_sse_comments_multiline_done(self):
        parser=SSEParser()
        for line in [b': hi\n',b'data: {"x":\n',b'data: 1}\n']:self.assertIsNone(parser.line(line))
        self.assertEqual(json.loads(parser.line(b'\n')),{'x':1})
        parser.line(b'data: [DONE]\r\n');self.assertEqual(parser.line(b'\r\n'),'[DONE]')


class FakeBackend:
    def __init__(self):
        self.data={role:state(role) for role in ROLES};self.gates={};self.cancelled=set();self.calls=[];self.fail=None
    async def open(self):pass
    async def close(self):pass
    async def snapshot(self,role):
        if self.fail==role:raise OSError('node unavailable')
        return copy.deepcopy(self.data[role])
    async def cancel(self,role,rid):
        self.calls.append((role,rid));self.cancelled.add(rid)
        self.gates.setdefault((role,rid),asyncio.Event()).set()
        return dict(rid=rid,cancel_enqueued=True,cleanup_confirmed=False)
    async def read(self,role,job):
        gate=self.gates.setdefault((role,job.engine_id),asyncio.Event())
        meta=dict(id=job.engine_id,prompt_tokens=job.prompt_tokens,completion_tokens=1,cached_tokens=0,finish_reason=None)
        yield dict(text='x',meta_info=meta)
        await gate.wait()
        reason=dict(type='abort',status_code=499) if job.engine_id in self.cancelled else dict(type='length')
        count=1 if role=='prefill' else job.output_limit
        yield dict(text='x'*count,meta_info={**meta,'completion_tokens':count,'finish_reason':reason})
    def done(self,job):
        for role in ROLES:self.gates.setdefault((role,job.engine_id),asyncio.Event()).set()
    def ack(self,job,role=None):
        for kind in [role] if role else ROLES:
            self.data[kind]['requests'].append(dict(rid=job.engine_id,bootstrap_room=job.room,
                cleanup_confirmed=True,compute_pending=0,allocator_release_seen=True,metadata_release_seen=True,
                cleanup_scope='cooperative_cancel' if job.cancel_requested else 'normal_success'))


async def ticks():
    for _ in range(8):await asyncio.sleep(0)


def run_async(coro):
    if os.name!='nt':return asyncio.run(coro)
    # CPU tests require no IO. Avoid creating a Windows loopback socket in the sandbox.
    class Headless(asyncio.SelectorEventLoop):
        def _make_self_pipe(self):self._ssock=self._csock=None
        def _close_self_pipe(self):pass
    loop=Headless()
    loop._selector.select=lambda timeout: (time.sleep(min(timeout or 0,.01)) or [])
    try:return loop.run_until_complete(coro)
    finally:loop.close()


class LifecycleTests(unittest.TestCase):
    def check(self,scenario,**settings):
        async def run():
            loop=asyncio.get_running_loop()
            unhandled=[]
            loop.set_exception_handler(lambda loop,context:unhandled.append(context))
            backend=FakeBackend()
            async def encode(prompt):return [1]*len(prompt)
            service=Service(backend,encode,Config(active_limit=1,**settings))
            await service.start(monitor=False)
            try:await scenario(service,backend)
            finally:
                service.closing=True
                tasks=list(service.tasks)
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                await ticks()
                gc.collect()
                await ticks()
                self.assertFalse(unhandled,repr(unhandled))
        run_async(run())

    def test_response_does_not_release_budget(self):
        async def scenario(s,b):
            job=await s.submit({'prompt':'x','max_tokens':4},'one');await ticks();b.done(job);await job.finished.wait()
            self.assertTrue(s.ledger.held(job));self.assertEqual(job.state,'completed')
            second=await s.submit({'prompt':'y'},'two');self.assertEqual(second.state,'queued')
            b.ack(job,'prefill');await s.poll();self.assertTrue(s.ledger.held(job));self.assertEqual(second.state,'queued')
            b.ack(job,'decode');await s.poll();self.assertFalse(s.ledger.held(job));self.assertEqual(second.state,'dispatched')
        self.check(scenario)

    def test_queued_cancel_and_overflow(self):
        async def scenario(s,b):
            first=await s.submit({'prompt':'x'},'first')
            queued=await s.submit({'prompt':'x'},'queued')
            with self.assertRaises(ServeError) as result:await s.submit({'prompt':'x'},'overflow')
            self.assertEqual(result.exception.status,429)
            await s.cancel('queued');self.assertEqual(queued.error.status,499)
            self.assertFalse(s.ledger.held(queued));self.assertEqual(b.calls,[])
            await s.cancel('queued');self.assertEqual(b.calls,[])
        self.check(scenario,queue_limit=1)

    def test_active_cancel_disconnect_and_late_cancel(self):
        async def scenario(s,b):
            job=await s.submit({'prompt':'x'},'id');await ticks()
            await s.client_disconnected(job);await job.finished.wait();await ticks()
            self.assertEqual(job.error.status,499);self.assertEqual(len(b.calls),2);self.assertTrue(s.ledger.held(job))
            await s.cancel('id');self.assertEqual(len(b.calls),2)
            b.ack(job);await s.poll();self.assertFalse(s.ledger.held(job))
        self.check(scenario)

    def test_queue_deadline(self):
        async def scenario(s,b):
            await s.submit({'prompt':'x'},'one');job=await s.submit({'prompt':'y'},'two')
            job.enqueued_at-=31;s.dispatch();self.assertEqual(job.error.status,504);self.assertFalse(s.ledger.held(job))
        self.check(scenario)

    def test_node_loss_retains_budget_and_recovery_needs_new_pair(self):
        async def scenario(s,b):
            job=await s.submit({'prompt':'x'},'id');await ticks();b.fail='decode'
            with self.assertRaises(OSError):await s.poll()
            s.quarantine('node unavailable');await ticks()
            self.assertFalse(s.ready);self.assertTrue(s.ledger.held(job));self.assertEqual(job.error.status,503)
            with self.assertRaises(ServeError):await s.submit({'prompt':'x'},'new')
            b.fail=None
            with self.assertRaises(ServeError):await s.recover(True)
            for role in ROLES:b.data[role]['epoch']=role+'-epoch2'
            with self.assertRaises(ServeError):await s.recover(False)
            await s.recover(True);self.assertTrue(s.ready);self.assertFalse(s.ledger.held(job))
            self.assertEqual(job.cleanup['decode']['kind'],'epoch_retired')
        self.check(scenario)

    def test_foreign_request_and_event_gap(self):
        async def scenario(s,b):
            b.data['prefill']['requests']=[dict(rid='foreign',cleanup_confirmed=False)]
            with self.assertRaises(ServeError):await s.poll()
            b.data['prefill']['requests']=[];b.data['decode']['events_oldest_seq']=3;b.data['decode']['events_latest_seq']=4
            with self.assertRaises(ServeError):await s.poll()
        self.check(scenario)

    def test_cleanup_deadline_and_invalid_ack(self):
        async def scenario(s,b):
            job=await s.submit({'prompt':'x'},'id');await ticks();b.done(job);await job.finished.wait()
            job.completed_at-=31
            with self.assertRaises(ServeError):await s.poll()
            job.completed_at=time.monotonic();b.ack(job);b.data['prefill']['requests'][0]['bootstrap_room']=999
            with self.assertRaises(ValueError):await s.poll()
            self.assertTrue(s.ledger.held(job))
        self.check(scenario)

    def test_registry_capacity_duplicates_context(self):
        async def scenario(s,b):
            await s.submit({'prompt':'x'},'id')
            with self.assertRaises(ServeError) as result:await s.submit({'prompt':'x'},'id')
            self.assertEqual(result.exception.status,409)
            with self.assertRaises(ServeError) as result:await s.submit({'prompt':'x'},'next')
            self.assertEqual(result.exception.status,429)
        self.check(scenario,history=1)

    def test_cancel_drain_deadline(self):
        async def scenario(s,b):
            job=await s.submit({'prompt':'x'},'id');await ticks()
            job.cancel_requested=True;job.cancelled_at=time.monotonic()-31
            with self.assertRaises(ServeError):await s.poll()
            self.assertTrue(s.ledger.held(job))
        self.check(scenario)

    def test_stream_buffer_backpressure_and_disconnect(self):
        async def scenario(s,b):
            job=await s.submit({'prompt':'x','stream':True,'max_tokens':4},'id');await ticks()
            self.assertEqual(job.buffer.qsize(),1)
            b.done(job);await ticks()
            self.assertFalse(job.finished.is_set())
            await s.client_disconnected(job);await job.finished.wait();await ticks()
            self.assertTrue(job.client_gone);self.assertEqual(job.state,'cancelled')
            self.assertTrue(s.ledger.held(job));b.ack(job);await s.poll();self.assertFalse(s.ledger.held(job))
        self.check(scenario,stream_buffer=1)

    def test_epoch_change_and_guard_fault_require_quarantine(self):
        async def scenario(s,b):
            job=await s.submit({'prompt':'x'},'id');await ticks()
            b.data['prefill']['epoch']='changed'
            with self.assertRaises(ServeError):await s.poll()
            b.data['prefill']['epoch']='prefill-epoch1';b.data['decode']['fault_guard']['quarantined']=True
            with self.assertRaises(ServeError):await s.poll()
            self.assertTrue(s.ledger.held(job))
        self.check(scenario)

    def test_complete_late_cancel_and_registry_retirement(self):
        async def scenario(s,b):
            job=await s.submit({'prompt':'x','max_tokens':4},'one');await ticks();b.done(job);await job.finished.wait()
            await s.cancel('one');self.assertFalse(job.cancel_requested);self.assertEqual(b.calls,[])
            b.ack(job);await s.poll();job.reply_closed=True
            other=await s.submit({'prompt':'x'},'two')
            self.assertNotIn('one',s.jobs);self.assertEqual(len(s.jobs),1)
        self.check(scenario,history=1)

    def test_phase_events_metrics_and_invalid_identity(self):
        async def scenario(s,b):
            job=await s.submit({'prompt':'x'},'id');await ticks()
            native=[]
            for seq,(stage,ns) in enumerate([('prefill_started',1_000_000_000),('prefill_completed',1_100_000_000)],1):
                native.append(dict(epoch='prefill-epoch1',seq=seq,rid=job.engine_id,bootstrap_room=job.room,stage=stage,monotonic_ns=ns))
            b.data['prefill'].update(events=native,events_latest_seq=2)
            await s.poll()
            self.assertIn('pdserve_prefill_compute_seconds_sum',s.telemetry.render(s))
            self.assertEqual(s.telemetry.histograms[('prefill_compute','short')][0],1)
            await s.poll();self.assertEqual(s.telemetry.histograms[('prefill_compute','short')][0],1)
            b.data['prefill']['events'].append({**native[0],'seq':3,'bootstrap_room':999})
            b.data['prefill']['events_latest_seq']=3
            with self.assertRaises(ServeError):await s.poll()
        self.check(scenario)

    def test_parent_cancel_drains_original_gather(self):
        async def scenario(s,b):
            job=await s.submit({'prompt':'x'},'id');await ticks()
            job.task.cancel()
            results=await asyncio.gather(job.task,return_exceptions=True)
            self.assertIsInstance(results[0],asyncio.CancelledError)
            self.assertTrue(s.ledger.held(job))
            await ticks();gc.collect()
        self.check(scenario)

    def test_upstream_error_does_not_cancel_own_cleanup(self):
        async def scenario(s,b):
            original=b.read
            async def malformed(role,job):
                if role=='decode':
                    yield dict(text='x',meta_info=dict(id='wrong-id'))
                else:
                    async for frame in original(role,job):yield frame
            b.read=malformed
            job=await s.submit({'prompt':'x'},'id')
            await job.task
            self.assertEqual(job.error.status,502)
            self.assertFalse(s.ready)
            self.assertTrue(s.ledger.held(job))
            await ticks()
        self.check(scenario)

    def test_generation_timeout_drains_both_readers(self):
        async def scenario(s,b):
            job=await s.submit({'prompt':'x'},'id')
            await job.task
            self.assertEqual(job.error.status,502)
            self.assertFalse(s.ready)
            self.assertTrue(s.ledger.held(job))
            await ticks()
        self.check(scenario,generation_timeout=.001)


if __name__=='__main__':unittest.main(verbosity=2)
