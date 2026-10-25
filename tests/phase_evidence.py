"""Phase and cleanup proof checks, independent of HTTP, CUDA and imports."""
import json
from pathlib import Path

ROLES=('prefill','decode')


class Tail:
    """Read only newly appended complete lines; keep partial writes for later."""
    def __init__(self,path,epoch=None,role=None):
        self.path=Path(path);self.epoch=epoch;self.role=role
        if self.path.is_symlink() or not self.path.is_file():raise ValueError('Unsafe or missing event log')
        self.handle=self.path.open('rb');self.pending=b'';self.rows=[]
        self.handle.seek(0,2)
        if self.handle.tell():
            self.handle.seek(-1,2)
            if self.handle.read(1)!=b'\n':raise ValueError('Initial idle event log has an incomplete last line')
        self.offset=self.handle.tell()

    def poll(self):
        if self.path.stat().st_size<self.offset:raise ValueError('Event log truncated during acceptance')
        data=self.pending+self.handle.read();self.offset=self.handle.tell()
        lines=data.split(b'\n');self.pending=lines.pop()
        added=[]
        for line in lines:
            if not line:raise ValueError('Empty event log line')
            row=json.loads(line)
            if self.epoch is not None and (row['epoch']!=self.epoch or row['role']!=self.role):
                raise ValueError('Native event log epoch/role changed')
            if self.rows and self.epoch is not None and row['seq']!=self.rows[-1]['seq']+1:
                raise ValueError('Native event sequence gap')
            self.rows.append(row);added.append(row)
        return added

    def close(self):self.handle.close()


def job(state,rid):
    rows=[r for r in state['requests'] if r['request_id']==rid]
    if len(rows)!=1:raise ValueError('Gateway request missing or duplicated')
    return rows[0]


def record(state,rid,room):
    rows=[r for r in state['requests'] if r['rid']==rid]
    if len(rows)!=1 or rows[0]['bootstrap_room']!=room:raise ValueError('Native request ID/room mismatch')
    return rows[0]


def phase_hit(phase,events,receipts):
    role='decode' if phase=='decode' else 'prefill'
    receipt=receipts.get(role)
    if not isinstance(receipt,dict) or type(receipt.get('issued_ns')) is not int:
        raise ValueError('Native timestamped cancel receipt required')
    issued=receipt['issued_ns']
    start,end={'prefill':('prefill_started','prefill_completed'),
               'transfer':('rdma_chunk_started','rdma_chunk_finished'),
               'decode':('decode_started','terminal')}[phase]
    opened=[]
    for row in events[role]:
        if row['stage']==start:opened.append(row['monotonic_ns'])
        elif row['stage']==end and opened:
            begin=opened.pop(0)
            # Strictly interior. Boundary equality or a missing end is no proof.
            if begin<issued<row['monotonic_ns']:return True
    return False


def cancelled_response(row):
    if row.get('client_error'):raise ValueError('Client failed instead of receiving cancellation')
    if row['status']==499:
        if row.get('response',{}).get('error',{}).get('code')!=499:
            raise ValueError('Expected explicit HTTP 499 cancellation body')
    elif row['status']==200:
        errors=[r for r in row['frames'] if 'error' in r]
        if not row['done'] or len(errors)!=1 or errors[0]['error'].get('code')!=499:
            raise ValueError('Started SSE must contain one cancellation error and DONE')
    else:raise ValueError('Cancellation had unexpected HTTP status')


def validate_active(state,rid,events,gateway_events,response):
    current=job(state,rid);cancelled_response(response)
    if current['state']!='cancelled' or not current['cancel_requested'] or current['budget_held']:
        raise ValueError('Cancelled request must reach terminal and release confirmed budgets')
    native={};receipts={}
    terminals=[r for r in gateway_events if r.get('request_id')==rid and r['event']=='terminal']
    if len(terminals)!=1 or terminals[0]['state']!='cancelled':raise ValueError('Duplicate or wrong terminal event')
    for role in ROLES:
        node=state['engines'][role];ack=current['cleanup'].get(role)
        if ack!={'kind':'engine_ack','epoch':node['epoch'],'scope':ack.get('scope') if isinstance(ack,dict) else None}:
            raise ValueError('Actual native cleanup ACK required; retired epoch is insufficient')
        row=record(node,current['engine_id'],current['bootstrap_room']);native[role]=row
        if (not row['cleanup_confirmed'] or row['compute_pending']!=0
                or not row['allocator_release_seen'] or not row['metadata_release_seen']):
            raise ValueError('Native allocator/metadata/compute cleanup incomplete')
        times=row['first_stage_ns'];confirmed=times['cleanup_confirmed']
        if any(times.get(k,float('inf'))>confirmed for k in ('terminal','allocator_released','metadata_released')):
            raise ValueError('Native ACK predates resource release')
        if (row.get('last_compute_result_ns') or 0)>confirmed:raise ValueError('Late compute result after ACK')
        if row['cleanup_scope']!=ack['scope']:raise ValueError('Gateway/native cleanup scopes differ')
        if role=='decode':
            if row['cleanup_scope']!='cooperative_cancel' or row['finish_reason'].get('type')!='abort':
                raise ValueError('Decode did not cooperatively abort')
            if times.get('cancel_armed',-1)<times.get('kv_ready',0):raise ValueError('Decode abort armed before KV ready')
        elif row['cleanup_scope'] not in ('cooperative_cancel','normal_success'):
            raise ValueError('Prefill did not safely drain')
        selected=[e for e in events[role] if e['rid']==current['engine_id']]
        if any(e['bootstrap_room']!=current['bootstrap_room'] for e in selected):raise ValueError('Event room mismatch')
        acks=[e for e in selected if e['stage']=='cleanup_confirmed']
        if len(acks)!=1 or acks[0]['monotonic_ns']!=confirmed:raise ValueError('Native cleanup event missing or repeated')
        if any(e['monotonic_ns']>confirmed for e in selected if e['stage'] in ('rdma_chunk_finished','compute_result_processed')):
            raise ValueError('Work completed after native ACK')
        releases=[e for e in gateway_events if e.get('request_id')==rid and e['event']=='budget_released' and e['role']==role]
        if len(releases)!=1 or releases[0]['confirmation']!=ack or releases[0]['monotonic_ns']<confirmed:
            raise ValueError('Budget released without or before actual ACK')
        receipt_rows=[e for e in gateway_events if e.get('request_id')==rid and e['event']=='cancel_receipt' and e['role']==role]
        if len(receipt_rows)!=1:raise ValueError('Duplicate or missing propagated cancellation')
        receipt=receipt_rows[0]['receipt'];receipts[role]=receipt
        if (not isinstance(receipt,dict) or receipt.get('rid')!=current['engine_id']
                or receipt.get('cancel_enqueued') is not True or receipt.get('cleanup_confirmed') is not False
                or receipt.get('mode')!='drain_prefill_and_transfer' or type(receipt.get('issued_ns')) is not int):
            raise ValueError('Invalid native intent receipt')
        if row['cancel_requested'] and row['cancel_issued_ns']!=receipt['issued_ns']:
            raise ValueError('Native first cancellation timestamp changed')
    return native,receipts


def validate_queued(state,rid,events,gateway_events,response):
    current=job(state,rid);cancelled_response(response)
    if current['state']!='cancelled' or not current['cancel_requested'] or current['budget_held'] or current['cleanup']:
        raise ValueError('Queued cancellation must hold no leases or native ACKs')
    for role in ROLES:
        if any(r['rid']==current['engine_id'] for r in state['engines'][role]['requests']):
            raise ValueError('Queued cancelled request reached native scheduler')
        if any(e['rid']==current['engine_id'] for e in events[role]):raise ValueError('Queued request has native events')
    own=[e for e in gateway_events if e.get('request_id')==rid]
    if len([e for e in own if e['event']=='terminal'])!=1:raise ValueError('Queued terminal repeated')
    if any(e['event'] in ('dispatched','cancel_receipt','budget_released') for e in own):
        raise ValueError('Queued request dispatched or propagated to native')


def frozen(job):
    return {k:job[k] for k in ('engine_id','bootstrap_room','state','completion_tokens','budget_held','cleanup')}
