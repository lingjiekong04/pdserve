"""Synthetic phase/ACK evidence and append-only files; no network or GPUs."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from phase_evidence import Tail,phase_hit,validate_active,validate_queued,cancelled_response,ROLES


class EvidenceTests(unittest.TestCase):
    def fixture(self):
        current=dict(request_id='client',engine_id='native',bootstrap_room=7,state='cancelled',
                     cancel_requested=True,budget_held=False,cleanup={},completion_tokens=1)
        state=dict(requests=[current],engines={});events={};gateway=[dict(event='terminal',request_id='client',state='cancelled')]
        response=dict(status=200,frames=[{'error':{'code':499}}],done=True)
        receipts={}
        for role in ROLES:
            scope='cooperative_cancel';ack=dict(kind='engine_ack',epoch=role+'-epoch',scope=scope)
            current['cleanup'][role]=ack
            issued=20 if role=='prefill' else 50
            times=dict(terminal=60,allocator_released=65,metadata_released=70,cleanup_confirmed=80,kv_ready=35,cancel_armed=50)
            record=dict(rid='native',bootstrap_room=7,cleanup_confirmed=True,compute_pending=0,
                        allocator_release_seen=True,metadata_release_seen=True,last_compute_result_ns=75,
                        cleanup_scope=scope,first_stage_ns=times,finish_reason={'type':'abort'},
                        cancel_requested=True,cancel_issued_ns=issued)
            state['engines'][role]=dict(epoch=role+'-epoch',requests=[record])
            phases=[('prefill_started',10),('prefill_completed',30),('rdma_chunk_started',31),('rdma_chunk_finished',39)] if role=='prefill' else [('decode_started',40),('terminal',60)]
            phases.append(('cleanup_confirmed',80))
            events[role]=[dict(rid='native',bootstrap_room=7,stage=k,monotonic_ns=t) for k,t in phases]
            receipt=dict(rid='native',issued_ns=issued,cancel_enqueued=True,cleanup_confirmed=False,mode='drain_prefill_and_transfer')
            receipts[role]=receipt
            gateway.extend([dict(event='budget_released',request_id='client',role=role,confirmation=ack,monotonic_ns=85),
                            dict(event='cancel_receipt',request_id='client',role=role,receipt=receipt)])
        return state,events,gateway,response,receipts

    def check(self,fixture):
        state,events,gateway,response,_=fixture
        return validate_active(state,'client',events,gateway,response)

    def test_actual_prefill_and_decode_receipts_inside_intervals(self):
        f=self.fixture();self.check(f)
        self.assertTrue(phase_hit('prefill',f[1],f[4]));self.assertTrue(phase_hit('decode',f[1],f[4]))
        self.assertFalse(phase_hit('transfer',f[1],f[4]))

    def test_transfer_repeated_chunks_accept_only_completed_enclosing_pair(self):
        f=self.fixture();f[4]['prefill']['issued_ns']=37
        self.assertTrue(phase_hit('transfer',f[1],f[4]))
        f[1]['prefill'].append(dict(stage='rdma_chunk_started',monotonic_ns=90))
        f[4]['prefill']['issued_ns']=95
        self.assertFalse(phase_hit('transfer',f[1],f[4]))

    def test_phase_boundary_equality_and_late_receipts_are_not_hits(self):
        f=self.fixture()
        for value in (10,30,31,100):
            f[4]['prefill']['issued_ns']=value;self.assertFalse(phase_hit('prefill',f[1],f[4]))

    def test_phase_receipt_must_be_native_timestamp_not_client_time(self):
        f=self.fixture();f[4]['prefill']={'client_issued_ns':20}
        with self.assertRaises(ValueError):phase_hit('prefill',f[1],f[4])

    def test_gateway_release_before_native_ack_is_rejected(self):
        f=self.fixture();f[2][1]['monotonic_ns']=79
        with self.assertRaises(ValueError):self.check(f)

    def test_epoch_retirement_is_not_cleanup_ack(self):
        f=self.fixture();f[0]['requests'][0]['cleanup']['decode']['kind']='epoch_retired'
        with self.assertRaises(ValueError):self.check(f)

    def test_pending_compute_or_allocator_release_blocks_acceptance(self):
        for key,value in (('compute_pending',1),('allocator_release_seen',False),('metadata_release_seen',False)):
            f=self.fixture();f[0]['engines']['decode']['requests'][0][key]=value
            with self.assertRaises(ValueError):self.check(f)

    def test_duplicate_native_ack_or_gateway_cancel_receipt_rejected(self):
        f=self.fixture();f[1]['decode'].append(copy.deepcopy(f[1]['decode'][-1]))
        with self.assertRaises(ValueError):self.check(f)
        f=self.fixture();f[2].append(copy.deepcopy(f[2][-1]))
        with self.assertRaises(ValueError):self.check(f)

    def test_event_room_mismatch_and_changed_cancel_timestamp_rejected(self):
        f=self.fixture();f[1]['decode'][0]['bootstrap_room']=9
        with self.assertRaises(ValueError):self.check(f)
        f=self.fixture();f[0]['engines']['decode']['requests'][0]['cancel_issued_ns']=51
        with self.assertRaises(ValueError):self.check(f)

    def test_prefill_normal_drain_allowed_decode_normal_completion_rejected(self):
        f=self.fixture();r=f[0]['engines']['prefill']['requests'][0];r['cleanup_scope']='normal_success';r['cancel_requested']=False
        f[0]['requests'][0]['cleanup']['prefill']['scope']='normal_success';self.check(f)
        f[0]['engines']['decode']['requests'][0]['cleanup_scope']='normal_success'
        with self.assertRaises(ValueError):self.check(f)

    def test_decode_abort_before_kv_ready_rejected(self):
        f=self.fixture();f[0]['engines']['decode']['requests'][0]['first_stage_ns']['cancel_armed']=34
        with self.assertRaises(ValueError):self.check(f)

    def test_work_after_ack_is_rejected(self):
        f=self.fixture();f[1]['prefill'].append(dict(rid='native',bootstrap_room=7,stage='rdma_chunk_finished',monotonic_ns=81))
        with self.assertRaises(ValueError):self.check(f)

    def test_http_and_sse_cancellation_errors_checked(self):
        cancelled_response(dict(status=499,response={'error':{'code':499}}))
        for row in (dict(status=200,frames=[{'error':{'code':499}}],done=False),
                    dict(status=200,frames=[{'error':{'code':502}}],done=True),dict(status=499,response={})):
            with self.assertRaises(ValueError):cancelled_response(row)

    def test_queued_cancellation_cannot_have_native_registration_or_dispatch(self):
        state,events,gateway,response,_=self.fixture()
        state['requests'][0]['cleanup']={}
        for role in ROLES:state['engines'][role]['requests']=[];events[role]=[]
        gateway=gateway[:1];validate_queued(state,'client',events,gateway,response)
        gateway.append(dict(event='dispatched',request_id='client'))
        with self.assertRaises(ValueError):validate_queued(state,'client',events,gateway,response)
        gateway.pop();state['engines']['decode']['requests']=[{'rid':'native'}]
        with self.assertRaises(ValueError):validate_queued(state,'client',events,gateway,response)


class TailTests(unittest.TestCase):
    def setUp(self):
        work=Path('work/stage2e1-tests');work.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=work);self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'events.jsonl';self.path.write_bytes(b'')
        self.tail=Tail(self.path,'epoch','prefill');self.addCleanup(self.tail.close)

    def append(self,row):
        with self.path.open('ab') as h:h.write(json.dumps(row).encode()+b'\n')

    def row(self,seq=1):return dict(epoch='epoch',role='prefill',seq=seq)

    def test_partial_json_write_is_retained_until_completed(self):
        data=json.dumps(self.row()).encode()
        self.path.write_bytes(data[:8]);self.assertEqual(self.tail.poll(),[])
        with self.path.open('ab') as h:h.write(data[8:]+b'\n')
        self.assertEqual(self.tail.poll(),[self.row()]);self.assertEqual(self.tail.poll(),[])

    def test_sequence_gap_and_epoch_change_rejected(self):
        self.append(self.row());self.tail.poll();self.append(self.row(3))
        with self.assertRaises(ValueError):self.tail.poll()
        self.path.write_bytes(b'');self.tail.handle.seek(0);self.tail.offset=0;self.tail.rows=[]
        row=self.row();row['epoch']='new';self.append(row)
        with self.assertRaises(ValueError):self.tail.poll()

    def test_truncation_is_not_silently_ignored(self):
        self.append(self.row());self.tail.poll();self.path.write_bytes(b'')
        with self.assertRaises(ValueError):self.tail.poll()

    def test_complete_corrupt_line_rejected(self):
        self.path.write_bytes(b'{invalid}\n')
        with self.assertRaises(ValueError):self.tail.poll()


if __name__=='__main__':unittest.main(verbosity=2)
