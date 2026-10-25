"""One event loop owns the queue, paired reservations and request lifecycle."""
import asyncio
from dataclasses import dataclass, field
import secrets
import time
from .core import Config, Ledger, ROLES, ServeError, WaitQueue, idle, validate_state
from .protocol import completion_options
from .telemetry import Telemetry


@dataclass(eq=False)
class Job:
    request_id: str
    engine_id: str
    room: int
    sequence: int
    input_ids: list
    output_limit: int
    stream: bool
    ignore_eos: bool
    temperature: float
    category: str
    enqueued_at: float
    prompt_tokens: int = 0
    state: str = 'queued'
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    dispatched_at: float = 0
    completed_at: float = 0
    output_tokens: int = 0
    text: str = ''
    transmitted_text: str = ''
    finish_reason: str = None
    error: object = None
    cancel_requested: bool = False
    cancelled_at: float = 0
    client_gone: bool = False
    reply_closed: bool = False
    task: object = None
    buffer: object = None
    cleanup: dict = field(default_factory=dict)
    phases: dict = field(default_factory=dict)
    first_token_at: float = 0
    measured_phases: set = field(default_factory=set)

    def public(self, ledger):
        return {'request_id': self.request_id, 'engine_id': self.engine_id, 'bootstrap_room': self.room,
                'state': self.state, 'class': self.category, 'prompt_tokens': self.prompt_tokens,
                'max_tokens': self.output_limit, 'completion_tokens': self.output_tokens,
                'cancel_requested': self.cancel_requested, 'client_gone': self.client_gone,
                'cleanup': self.cleanup, 'budget_held': ledger.held(self),
                'error': str(self.error) if self.error else None, 'phases': self.phases}


class Service:
    def __init__(self, backend, encoder, config=None, telemetry=None, model='pdserve-qwen'):
        self.config = config or Config()
        self.backend, self.encoder, self.model = backend, encoder, model
        self.telemetry = telemetry or Telemetry()
        self.queue = WaitQueue(self.config)
        self.jobs, self.latest, self.sequence = {}, {}, 0
        self.boot = secrets.token_hex(12)
        self.room_base = secrets.randbelow(1 << 61)
        self.ledger, self.ready, self.reason = None, False, 'starting'
        self.closing, self.monitor_task = False, None
        self.last_sequences, self.tokenizing, self.tasks = {}, set(), set()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def states(self):
        values = await asyncio.gather(*(self.backend.snapshot(role) for role in ROLES))
        return dict(zip(ROLES, values))

    async def start(self, monitor=True):
        await self.backend.open()
        states = await self.states()
        self.install(states)
        if monitor:
            self.monitor_task = asyncio.create_task(self.monitor())

    def install(self, states):
        ledger = Ledger(states, self.config)
        if not all(idle(state) for state in states.values()):
            raise ServeError(503, 'Exclusive healthy and fully idle P/D backends are required')
        self.ledger, self.latest = ledger, states
        self.last_sequences = {role: state['events_latest_seq'] for role, state in states.items()}
        self.ready, self.reason = True, None
        self.telemetry.emit('pair_enabled', budget=ledger.snapshot())

    def prune(self):
        for key, job in list(self.jobs.items()):
            if len(self.jobs) < self.config.history:
                break
            if job.finished.is_set() and job.reply_closed and not self.ledger.held(job):
                del self.jobs[key]

    async def submit(self, body, request_id):
        options = completion_options(body, self.model, request_id)
        if not self.ready or self.closing:
            raise ServeError(503, self.reason or 'Gateway is stopping')
        if request_id in self.jobs or request_id in self.tokenizing:
            raise ServeError(409, 'Request ID already exists')
        self.prune()
        if len(self.jobs) + len(self.tokenizing) >= self.config.history:
            raise ServeError(429, 'Request registry is full while cleanup or clients remain pending')
        if len(self.queue) + len(self.tokenizing) >= self.config.queue_limit:
            raise ServeError(429, 'Waiting queue is full')
        self.tokenizing.add(request_id)
        try:
            ids = await self.encoder(body['prompt'])
            if not self.ready or self.closing:
                raise ServeError(503, self.reason or 'Gateway is stopping')
            if not ids or len(ids) + options['output_limit'] > self.config.context:
                raise ServeError(413, 'Input plus maximum output exceeds the context limit')
            self.sequence += 1
            job = Job(request_id, f'pdserve-{self.boot}-{self.sequence}', self.room_base + self.sequence,
                      self.sequence, ids, category='short' if len(ids) <= self.config.short_limit else 'long',
                      enqueued_at=time.monotonic(), prompt_tokens=len(ids), **options)
            job.buffer = asyncio.Queue(self.config.stream_buffer)
            self.ledger.check_single(job)
            self.queue.add(job)
            self.jobs[request_id] = job
            self.telemetry.emit('queued', request_id=request_id, engine_id=job.engine_id,
                                room=job.room, prompt_tokens=len(ids), output_limit=job.output_limit,
                                category=job.category)
            self.dispatch()
            return job
        finally:
            self.tokenizing.discard(request_id)

    def finish(self, job, error=None):
        if job.finished.is_set():
            return
        job.error, job.completed_at = error, time.monotonic()
        job.state = 'cancelled' if error and error.status == 499 else 'failed' if error else 'completed'
        job.input_ids = []  # Reservations already retain their own token counts.
        job.started.set()
        job.finished.set()
        self.telemetry.counts[(job.state, job.category)] += 1
        self.telemetry.output_tokens += job.output_tokens
        self.telemetry.observe('gateway_e2e', job.category, job.completed_at - job.enqueued_at)
        self.telemetry.emit('terminal', request_id=job.request_id, state=job.state,
                            error=str(error) if error else None, budget_held=self.ledger.held(job))

    def dispatch(self):
        if not self.ready or self.closing:
            return
        now = time.monotonic()
        for job in self.queue.expired(now):
            self.queue.remove(job)
            self.finish(job, ServeError(504, 'Queue wait exceeded deadline'))
        while True:
            job = self.queue.choose(self.ledger, now)
            if job is None:
                break
            self.queue.remove(job)
            self.ledger.reserve(job)
            job.dispatched_at, job.state = time.monotonic(), 'dispatched'
            job.started.set()
            self.telemetry.observe('queue', job.category, job.dispatched_at - job.enqueued_at)
            self.telemetry.emit('dispatched', request_id=job.request_id, engine_id=job.engine_id,
                                budget=self.ledger.snapshot())
            job.task = self.spawn(self.run(job))

    async def put(self, job, chunk):
        while not job.client_gone and not job.finished.is_set() and not self.closing:
            try:
                await asyncio.wait_for(job.buffer.put(chunk), .2)
                return
            except asyncio.TimeoutError:
                pass

    async def read_role(self, role, job):
        terminal = False
        async for frame in self.backend.read(role, job):
            meta = frame.get('meta_info', {})
            if meta.get('id') != job.engine_id:
                raise ServeError(502, 'Upstream request ID mismatch')
            reason = meta.get('finish_reason')
            if reason and reason.get('type') == 'abort':
                raise ServeError(499 if job.cancel_requested else 502, 'Native request aborted: ' + str(reason))
            if role == 'decode':
                count, text = meta.get('completion_tokens'), frame.get('text')
                if (type(count) is not int or not job.output_tokens <= count <= job.output_limit
                        or meta.get('prompt_tokens') != job.prompt_tokens or meta.get('cached_tokens') != 0
                        or not isinstance(text, str)):
                    raise ServeError(502, 'Invalid native token accounting')
                # UTF-8 replacement characters at an incomplete token boundary can be revised.
                stable = job.text.rstrip('\ufffd')
                if not text.startswith(stable):
                    raise ServeError(502, 'Native cumulative text changed an already stable prefix')
                displayed = text if reason else text.rstrip('\ufffd')
                if not displayed.startswith(job.transmitted_text):
                    raise ServeError(502, 'Native text revised an already transmitted prefix')
                delta = displayed[len(job.transmitted_text):]
                job.transmitted_text = displayed
                if count > job.output_tokens and not job.first_token_at:
                    job.first_token_at = time.monotonic()
                    self.telemetry.observe('gateway_ttft', job.category, job.first_token_at - job.enqueued_at)
                job.text, job.output_tokens = text, count
                if reason:
                    job.finish_reason = reason['type']
                if job.stream:
                    await self.put(job, self.completion(job, delta, job.finish_reason))
            if reason:
                if reason.get('type') not in ('length', 'stop'):
                    raise ServeError(502, 'Unsupported native terminal reason')
                terminal = True
        if not terminal:
            raise ServeError(502, 'Native stream had no terminal frame')

    async def run(self, job):
        readers = [asyncio.create_task(self.read_role(role, job)) for role in ROLES]
        group = asyncio.gather(*readers)
        try:
            await asyncio.wait_for(group, self.config.generation_timeout)
            self.finish(job, ServeError(499, 'Request cancelled') if job.cancel_requested else None)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error = error if isinstance(error, ServeError) else ServeError(502, str(error))
            self.finish(job, error)
            if error.status != 499:
                self.quarantine(str(error))
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            # Python 3.10 wait_for cancellation can leave the original gathering
            # Future with CancelledError. Drain that Future as well as its children.
            await asyncio.shield(asyncio.gather(group, *readers, return_exceptions=True))

    def completion(self, job, text=None, reason=None):
        return {'id': job.request_id, 'object': 'text_completion', 'created': int(time.time()),
                'model': self.model, 'choices': [{'text': job.text if text is None else text,
                  'index': 0, 'finish_reason': reason if text is not None else job.finish_reason}],
                'usage': {'prompt_tokens': job.prompt_tokens, 'completion_tokens': job.output_tokens,
                          'total_tokens': job.prompt_tokens + job.output_tokens}}

    async def propagate_cancel(self, job):
        results = await asyncio.gather(*(self.backend.cancel(role, job.engine_id) for role in ROLES),
                                       return_exceptions=True)
        for role, result in zip(ROLES, results):
            self.telemetry.emit('cancel_receipt', request_id=job.request_id, role=role,
                                receipt=str(result) if isinstance(result, Exception) else result)
            if isinstance(result, Exception):
                self.quarantine(f'{role} cancel failed: {result}')

    async def cancel(self, request_id):
        job = self.get(request_id)
        if not job.finished.is_set() and not job.cancel_requested:
            job.cancel_requested = True
            job.cancelled_at = time.monotonic()
            self.telemetry.emit('cancel_requested', request_id=request_id)
            if not job.dispatched_at:
                self.queue.remove(job)
                self.finish(job, ServeError(499, 'Queued request cancelled before dispatch'))
            else:
                self.spawn(self.propagate_cancel(job))
        return job.public(self.ledger)

    async def client_disconnected(self, job):
        job.client_gone, job.reply_closed = True, True
        while not job.buffer.empty():
            job.buffer.get_nowait()
        await self.cancel(job.request_id)

    def get(self, request_id):
        if request_id not in self.jobs:
            raise ServeError(404, 'Unknown or retired request ID')
        return self.jobs[request_id]

    def quarantine(self, reason):
        if not self.ready:
            return
        self.ready, self.reason = False, reason
        self.telemetry.emit('pair_quarantined', reason=reason)
        for job in self.jobs.values():
            if not job.finished.is_set():
                self.queue.remove(job)
                self.finish(job, ServeError(503, 'P/D pair quarantined: ' + reason))
            if self.ledger.held(job):
                job.cancel_requested = True
                self.spawn(self.propagate_cancel(job))
                # run() quarantines on its own upstream error. Cancelling that
                # same task here would interrupt its finally cleanup.
                if job.task and not job.task.done() and job.task is not asyncio.current_task():
                    job.task.cancel()

    async def poll(self):
        states = await self.states()
        self.latest = states
        owned = {job.engine_id: job for job in self.jobs.values() if job.dispatched_at}
        for role, state in states.items():
            node = self.ledger.nodes[role]
            validate_state(state, role)
            if state['epoch'] != node.epoch or state['capacity'] != node.capacity:
                raise ServeError(503, role + ': epoch or capacity changed; explicit recovery required')
            previous = self.last_sequences[role]
            if state['events_oldest_seq'] > previous + 1 and state['events_latest_seq'] > previous:
                raise ServeError(503, role + ': observer event window was lost')
            for event in state['events']:
                if event['seq'] <= previous:
                    continue
                job = owned.get(event['rid'])
                if job:
                    if event['epoch'] != node.epoch or event['bootstrap_room'] != job.room:
                        raise ServeError(503, 'Mismatched phase event')
                    job.phases.setdefault(role, {}).setdefault(event['stage'], event['monotonic_ns'])
                    self.telemetry.emit('engine_event', request_id=job.request_id, native=event)
                    pairs = {'prefill': [('prefill_compute', 'prefill_started', 'prefill_completed'),
                                         ('prefill_to_transfer_done', 'prefill_completed', 'transfer_completed')],
                             'decode': [('decode_kv_wait', 'kv_preallocated', 'kv_ready'),
                                        ('decode_compute_window', 'decode_started', 'terminal')]}
                    times = job.phases[role]
                    for name, start, end in pairs[role]:
                        if start in times and end in times and name not in job.measured_phases:
                            value = (times[end] - times[start]) / 1e9
                            if value < 0:
                                raise ServeError(503, 'Engine phase clock ordering invalid')
                            self.telemetry.observe(name, job.category, value)
                            job.measured_phases.add(name)
            self.last_sequences[role] = state['events_latest_seq']
            for record in state['requests']:
                job = owned.get(record['rid'])
                if job is None and not record['cleanup_confirmed']:
                    raise ServeError(503, 'Foreign request found on exclusive backend')
                if job and record['cleanup_confirmed'] and self.ledger.release_ack(role, job, state['epoch'], record):
                    job.cleanup[role] = {'kind': 'engine_ack', 'epoch': node.epoch, 'scope': record['cleanup_scope']}
                    self.telemetry.emit('budget_released', request_id=job.request_id, role=role,
                                        confirmation=job.cleanup[role])
            if state['resources']['kv_used_tokens'] > node.used_tokens:
                # A snapshot may contain allocations for a just-acknowledged request only if the observer is inconsistent.
                raise ServeError(503, role + ': actual KV exceeds owned reservations')
        for job in self.jobs.values():
            if job.finished.is_set() and self.ledger.held(job) and time.monotonic() - job.completed_at > self.config.cleanup_timeout:
                raise ServeError(503, 'Engine cleanup acknowledgement exceeded deadline')
            if job.cancelled_at and self.ledger.held(job) and time.monotonic() - job.cancelled_at > self.config.cleanup_timeout:
                raise ServeError(503, 'Cooperative cancellation drain exceeded deadline')
        if self.telemetry.error:
            raise ServeError(503, 'Event log write failed: ' + self.telemetry.error)
        self.dispatch()

    async def monitor(self):
        while not self.closing:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.quarantine(str(error))
            await asyncio.sleep(self.config.poll_interval)

    async def recover(self, old_pair_stopped=False):
        if self.ready:
            raise ServeError(409, 'Pair is already enabled')
        states = await self.states()
        new_ledger = Ledger(states, self.config)
        if not all(idle(state) for state in states.values()):
            raise ServeError(503, 'Recovery requires a healthy fully idle pair')
        held = any(node.leases for node in self.ledger.nodes.values())
        if held and (old_pair_stopped is not True or any(states[role]['epoch'] == self.ledger.nodes[role].epoch for role in ROLES)):
            raise ServeError(409, 'Held old leases require both new epochs and operator confirmation that both old processes stopped')
        for job in self.jobs.values():
            for role in ROLES:
                if job.engine_id in self.ledger.nodes[role].leases:
                    job.cleanup[role] = {'kind': 'epoch_retired', 'old_epoch': self.ledger.nodes[role].epoch,
                                         'new_epoch': new_ledger.nodes[role].epoch, 'engine_ack': False}
                    self.telemetry.emit('old_epoch_retired', request_id=job.request_id, role=role,
                                        confirmation=job.cleanup[role])
        self.install(states)
        return self.state()

    def state(self):
        return {'ready': self.ready, 'reason': self.reason, 'queue': len(self.queue),
                'budget': self.ledger.snapshot() if self.ledger else {},
                'engines': self.latest, 'requests': [job.public(self.ledger) for job in self.jobs.values()],
                'automatic_process_restart': False}

    async def close(self):
        self.closing = True
        if self.monitor_task:
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        for job in self.jobs.values():
            if not job.finished.is_set():
                await self.cancel(job.request_id)
        deadline = time.monotonic() + 5
        while self.ledger and any(node.leases for node in self.ledger.nodes.values()) and time.monotonic() < deadline:
            try:
                await self.poll()
            except Exception:
                break
            await asyncio.sleep(.1)
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.telemetry.emit('gateway_stopped', budget=self.ledger.snapshot() if self.ledger else {})
        await self.backend.close()
        self.telemetry.close()
