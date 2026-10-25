"""Two independent mixed SGLang replicas behind a strict FIFO gateway."""
import asyncio
from collections import Counter, deque
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import secrets
import time


class ServeError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def validate_request(body, request_id, model='pdserve-qwen', context=16384):
    if not isinstance(body, dict):
        raise ServeError(400, 'Expected a JSON object')
    allowed = {'model', 'prompt', 'max_tokens', 'stream', 'temperature', 'n', 'ignore_eos'}
    if set(body) - allowed:
        raise ServeError(400, 'Unsupported fields: ' + ', '.join(sorted(set(body) - allowed)))
    if body.get('model', model) != model:
        raise ServeError(404, 'Unknown served model')
    if not isinstance(body.get('prompt'), str) or not body['prompt']:
        raise ServeError(400, 'prompt must be one nonempty text string')
    if len(body['prompt'].encode('utf-8')) > 1_048_576:
        raise ServeError(413, 'Prompt exceeds the 1 MiB text limit')
    output = body.get('max_tokens', 128)
    if type(output) is not int or output < 1:
        raise ServeError(400, 'max_tokens must be a positive integer')
    if type(body.get('n', 1)) is not int or body.get('n', 1) != 1:
        raise ServeError(400, 'Only n=1 is supported')
    stream, ignore = body.get('stream', False), body.get('ignore_eos', False)
    if type(stream) is not bool or type(ignore) is not bool:
        raise ServeError(400, 'stream and ignore_eos must be booleans')
    temperature = body.get('temperature', 0)
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ServeError(400, 'temperature must be finite and between 0 and 2')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', request_id):
        raise ServeError(400, 'Invalid X-Request-ID')
    return {'output_limit': output, 'stream': stream, 'ignore_eos': ignore,
            'temperature': temperature}


def parse_metrics(text):
    names = {'sglang:num_running_reqs': 'running', 'sglang:num_queue_reqs': 'waiting',
             'sglang:num_used_tokens': 'kv_used_tokens', 'sglang:token_usage': 'token_usage'}
    result = {}
    for raw in text.splitlines():
        if not raw or raw.startswith('#'):
            continue
        head, _, value = raw.rpartition(' ')
        base = head.split('{', 1)[0]
        if base in names:
            try:
                result[names[base]] = float(value)
            except ValueError:
                pass
    return result


class EventLog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a', encoding='utf-8', buffering=1)
        self.counts = Counter()

    def emit(self, event, **fields):
        row = {'event': event, 'monotonic_ns': time.monotonic_ns(),
               'unix_ns': time.time_ns(), **fields}
        self.handle.write(json.dumps(row, ensure_ascii=False) + '\n')

    def close(self):
        self.handle.close()


class NativeBackend:
    def __init__(self, urls, timeout=420):
        self.urls = urls
        self.timeout = timeout
        self.session = None

    async def open(self):
        import aiohttp
        self.aiohttp = aiohttp
        self.session = aiohttp.ClientSession(trust_env=False, read_bufsize=1_048_576,
            connector=aiohttp.TCPConnector(limit=256, limit_per_host=128),
            timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=self.timeout))

    async def close(self):
        if self.session:
            await self.session.close()

    async def snapshot(self, name):
        timeout = self.aiohttp.ClientTimeout(total=10)
        async with self.session.get(self.urls[name] + '/get_server_info', timeout=timeout) as response:
            response.raise_for_status()
            info = await response.json()
        async with self.session.get(self.urls[name] + '/metrics', timeout=timeout) as response:
            response.raise_for_status()
            metrics = parse_metrics(await response.text())
        expected = {'tp_size': 1, 'pp_size': 1, 'dp_size': 1, 'context_length': 16384,
                    'dtype': 'bfloat16', 'disable_radix_cache': True,
                    'disaggregation_mode': 'null', 'served_model_name': 'pdserve-qwen'}
        for key, value in expected.items():
            if info.get(key) != value:
                raise ServeError(503, f'{name}: unexpected engine setting {key}={info.get(key)}')
        capacity = info.get('max_total_num_tokens')
        if type(capacity) is not int or capacity < 16384:
            raise ServeError(503, name + ': invalid KV capacity')
        used = int(metrics.get('kv_used_tokens', 0))
        return {'healthy': True, 'capacity': {'kv_tokens': capacity, 'page_size': info.get('page_size')},
                'resources': {'kv_used_tokens': used, 'kv_free_tokens': capacity - used,
                              'queues': {'running': int(metrics.get('running', 0)),
                                         'waiting': int(metrics.get('waiting', 0))}},
                'settings': {key: info.get(key) for key in expected}}

    async def stream(self, name, job):
        body = {'rid': job.engine_id, 'input_ids': job.input_ids, 'stream': True,
                'sampling_params': {'temperature': job.temperature,
                    'ignore_eos': job.ignore_eos, 'max_new_tokens': job.output_limit,
                    'stream_interval': 1}}
        done = False
        async with self.session.post(self.urls[name] + '/generate', json=body) as response:
            response.raise_for_status()
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                raise ServeError(502, 'Native backend did not return SSE')
            data = []
            while True:
                raw = await response.content.readline()
                if not raw:
                    break
                line = raw.decode('utf-8').rstrip('\r\n')
                if not line:
                    if not data:
                        continue
                    payload, data = '\n'.join(data), []
                    if payload == '[DONE]':
                        done = True
                        break
                    value = json.loads(payload)
                    if not isinstance(value, dict) or 'error' in value:
                        raise ServeError(502, 'Native error frame: ' + str(value))
                    yield value
                elif line.startswith('data:'):
                    data.append(line[5:].lstrip(' '))
        if not done:
            raise ServeError(502, 'Native stream ended without DONE')

    async def abort(self, name, engine_id):
        timeout = self.aiohttp.ClientTimeout(total=5)
        async with self.session.post(self.urls[name] + '/abort_request',
                                     json={'rid': engine_id}, timeout=timeout) as response:
            if response.status != 200:
                raise ServeError(502, f'{name}: native abort failed with {response.status}')


@dataclass(eq=False)
class Job:
    request_id: str
    engine_id: str
    sequence: int
    input_ids: list
    output_limit: int
    stream: bool
    ignore_eos: bool
    temperature: float
    category: str
    enqueued_ns: int
    prompt_tokens: int
    state: str = 'queued'
    replica: str = None
    output_tokens: int = 0
    text: str = ''
    transmitted_text: str = ''
    finish_reason: str = None
    error: object = None
    reply_closed: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    buffer: object = None
    task: object = None


class FIFOService:
    def __init__(self, backend, encoder, telemetry, active_per_replica=1,
                 queue_limit=8, queue_timeout=30, context=16384):
        if active_per_replica < 1 or queue_limit < 1:
            raise ValueError('Positive limits required')
        self.backend, self.encoder, self.telemetry = backend, encoder, telemetry
        self.active_limit, self.queue_limit = active_per_replica, queue_limit
        self.queue_timeout, self.context = queue_timeout, context
        self.names = tuple(backend.urls)
        self.queue, self.jobs, self.nodes = deque(), {}, {}
        self.tokenizing = set()
        self.sequence, self.rr = 0, 0
        self.boot = secrets.token_hex(12)
        self.ready, self.reason, self.closing = False, 'starting', False
        self.latest, self.monitor_task, self.tasks = {}, None, set()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def start(self):
        await self.backend.open()
        values = await asyncio.gather(*(self.backend.snapshot(name) for name in self.names))
        self.latest = dict(zip(self.names, values))
        for name, value in self.latest.items():
            if value['resources']['kv_used_tokens'] or any(value['resources']['queues'].values()):
                raise ServeError(503, name + ': exclusive idle replica required')
            self.nodes[name] = {'token_limit': math.floor(value['capacity']['kv_tokens'] * .9),
                                'slot_limit': self.active_limit, 'reserved_tokens': 0,
                                'reserved_slots': 0, 'leases': {}}
        self.ready, self.reason = True, None
        self.telemetry.emit('baseline_enabled', topology='two_mixed_fifo', budget=self.budget())
        self.monitor_task = asyncio.create_task(self.monitor())

    def budget(self):
        return {name: {key: value for key, value in node.items() if key != 'leases'}
                for name, node in self.nodes.items()}

    async def submit(self, body, request_id):
        options = validate_request(body, request_id, context=self.context)
        if not self.ready or self.closing:
            raise ServeError(503, self.reason or 'Gateway is stopping')
        if request_id in self.jobs or request_id in self.tokenizing:
            raise ServeError(409, 'Request ID already exists')
        if len(self.queue) + len(self.tokenizing) >= self.queue_limit:
            raise ServeError(429, 'Waiting queue is full')
        self.tokenizing.add(request_id)
        try:
            input_ids = await self.encoder(body['prompt'])
            if not self.ready or self.closing:
                raise ServeError(503, self.reason or 'Gateway is stopping')
            if not input_ids or len(input_ids) + options['output_limit'] >= self.context:
                raise ServeError(413, 'Input plus maximum output must be strictly less than context limit')
            need = len(input_ids) + options['output_limit']
            if all(need > node['token_limit'] for node in self.nodes.values()):
                raise ServeError(413, 'Request exceeds one replica KV budget')
            self.sequence += 1
            job = Job(request_id, f'baseline-{self.boot}-{self.sequence}', self.sequence,
                      input_ids, category='short' if len(input_ids) <= 2048 else 'long',
                      enqueued_ns=time.monotonic_ns(), prompt_tokens=len(input_ids), **options)
            job.buffer = asyncio.Queue(128)
            self.jobs[request_id] = job
            self.queue.append(job)
            self.telemetry.emit('queued', request_id=request_id, engine_id=job.engine_id,
                                sequence=job.sequence, prompt_tokens=job.prompt_tokens,
                                output_limit=job.output_limit, category=job.category)
            self.dispatch()
            return job
        finally:
            self.tokenizing.discard(request_id)

    def available(self, job):
        need = job.prompt_tokens + job.output_limit
        result = []
        for offset in range(len(self.names)):
            index = (self.rr + offset) % len(self.names)
            name = self.names[index]
            node = self.nodes[name]
            if (node['reserved_slots'] < node['slot_limit'] and
                    node['reserved_tokens'] + need <= node['token_limit']):
                result.append((index, name))
        return result

    def dispatch(self):
        if not self.ready or self.closing:
            return
        now = time.monotonic_ns()
        while self.queue and (now - self.queue[0].enqueued_ns) / 1e9 > self.queue_timeout:
            self.finish(self.queue.popleft(), ServeError(504, 'Queue wait exceeded deadline'))
        while self.queue:
            job = self.queue[0]
            choices = self.available(job)
            if not choices:
                break
            index, name = choices[0]
            self.rr = (index + 1) % len(self.names)
            self.queue.popleft()
            node = self.nodes[name]
            need = job.prompt_tokens + job.output_limit
            node['reserved_slots'] += 1
            node['reserved_tokens'] += need
            node['leases'][job.engine_id] = need
            job.replica, job.state = name, 'dispatched'
            job.started.set()
            self.telemetry.emit('dispatched', request_id=job.request_id,
                                engine_id=job.engine_id, replica=name, budget=self.budget())
            job.task = self.spawn(self.run(job))

    def finish(self, job, error=None):
        if job.finished.is_set():
            return
        if job.replica:
            node = self.nodes[job.replica]
            need = node['leases'].pop(job.engine_id, 0)
            node['reserved_tokens'] -= need
            node['reserved_slots'] -= int(bool(need))
        job.error = error
        job.state = 'cancelled' if error and error.status == 499 else 'failed' if error else 'completed'
        job.input_ids = []
        job.started.set()
        job.finished.set()
        self.telemetry.counts[job.state] += 1
        self.telemetry.emit('terminal', request_id=job.request_id, state=job.state,
                            replica=job.replica, error=str(error) if error else None,
                            completion_tokens=job.output_tokens, budget=self.budget())
        self.dispatch()

    async def put(self, job, value):
        await job.buffer.put(value)

    def completion(self, job, text=None, reason=None):
        return {'id': job.request_id, 'object': 'text_completion', 'created': int(time.time()),
                'model': 'pdserve-qwen',
                'choices': [{'text': job.text if text is None else text, 'index': 0,
                             'finish_reason': reason if text is not None else job.finish_reason}],
                'usage': {'prompt_tokens': job.prompt_tokens,
                          'completion_tokens': job.output_tokens,
                          'total_tokens': job.prompt_tokens + job.output_tokens}}

    async def run(self, job):
        terminal = False
        try:
            async for value in self.backend.stream(job.replica, job):
                meta = value.get('meta_info', {})
                if meta.get('id') != job.engine_id:
                    raise ServeError(502, 'Native request ID mismatch')
                count, text, reason = meta.get('completion_tokens'), value.get('text'), meta.get('finish_reason')
                if (type(count) is not int or not job.output_tokens <= count <= job.output_limit
                        or meta.get('prompt_tokens') != job.prompt_tokens
                        or meta.get('cached_tokens') != 0 or not isinstance(text, str)):
                    raise ServeError(502, 'Invalid native token accounting')
                stable = job.text.rstrip('\ufffd')
                if not text.startswith(stable):
                    raise ServeError(502, 'Native cumulative text changed a stable prefix')
                displayed = text if reason else text.rstrip('\ufffd')
                if not displayed.startswith(job.transmitted_text):
                    raise ServeError(502, 'Native text revised a transmitted prefix')
                delta = displayed[len(job.transmitted_text):]
                job.text, job.transmitted_text, job.output_tokens = text, displayed, count
                finish = reason.get('type') if isinstance(reason, dict) else None
                if finish:
                    if finish not in ('length', 'stop'):
                        raise ServeError(502, 'Unsupported native finish reason')
                    job.finish_reason, terminal = finish, True
                if job.stream:
                    await self.put(job, self.completion(job, delta, finish))
            if not terminal:
                raise ServeError(502, 'Native stream had no terminal frame')
            self.finish(job)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.finish(job, error if isinstance(error, ServeError) else ServeError(502, str(error)))

    async def cancel(self, job):
        if job.finished.is_set():
            return
        if job in self.queue:
            self.queue.remove(job)
            self.finish(job, ServeError(499, 'Queued request cancelled'))
        else:
            try:
                await self.backend.abort(job.replica, job.engine_id)
            finally:
                self.finish(job, ServeError(499, 'Active request cancelled'))

    async def poll(self):
        values = await asyncio.gather(*(self.backend.snapshot(name) for name in self.names))
        self.latest = dict(zip(self.names, values))
        self.dispatch()

    async def monitor(self):
        while not self.closing:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.ready, self.reason = False, str(error)
                self.telemetry.emit('baseline_quarantined', reason=self.reason)
            await asyncio.sleep(.5)

    def state(self):
        return {'ready': self.ready, 'reason': self.reason, 'queue': len(self.queue),
                'topology': 'two_mixed_replicas_fifo', 'scheduler': 'strict_fifo',
                'gateway_instance_id': self.boot, 'gateway_event_log': str(self.telemetry.path),
                'active_per_replica': self.active_limit, 'queue_limit': self.queue_limit,
                'budget': self.budget(), 'engines': self.latest,
                'requests': [{'request_id': j.request_id, 'state': j.state,
                              'replica': j.replica, 'prompt_tokens': j.prompt_tokens,
                              'max_tokens': j.output_limit} for j in self.jobs.values()]}

    def metrics(self):
        lines = ['# TYPE pdserve_ready gauge', f'pdserve_ready {int(self.ready)}',
                 '# TYPE pdserve_queue_requests gauge', f'pdserve_queue_requests {len(self.queue)}',
                 '# TYPE pdserve_requests_total counter']
        for state, count in sorted(self.telemetry.counts.items()):
            lines.append(f'pdserve_requests_total{{status="{state}"}} {count}')
        for name, node in self.nodes.items():
            lines.append(f'pdserve_gateway_reserved_tokens{{replica="{name}"}} {node["reserved_tokens"]}')
            lines.append(f'pdserve_gateway_reserved_slots{{replica="{name}"}} {node["reserved_slots"]}')
        return '\n'.join(lines) + '\n'

    async def close(self):
        self.closing, self.ready = True, False
        if self.monitor_task:
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        for job in list(self.jobs.values()):
            if not job.finished.is_set():
                await self.cancel(job)
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await self.backend.close()
        self.telemetry.close()


def error_object(error):
    return {'error': {'message': str(error), 'type': 'pdserve_error', 'code': error.status}}


def build_app(service):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    globals()['Request'] = Request

    @asynccontextmanager
    async def lifespan(app):
        try:
            await service.start()
            yield
        finally:
            await service.close()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(ServeError)
    async def failure(request, error):
        return JSONResponse(error_object(error), status_code=error.status)

    async def read_body(request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 2_097_152:
                raise ServeError(413, 'Request exceeds 2 MiB')
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise ServeError(400, 'Invalid JSON')

    async def events(job):
        while not job.finished.is_set() or not job.buffer.empty():
            try:
                value = await asyncio.wait_for(job.buffer.get(), .1)
                yield 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'
            except asyncio.TimeoutError:
                continue
        if job.error:
            yield 'data: ' + json.dumps(error_object(job.error), ensure_ascii=False) + '\n\n'
        yield 'data: [DONE]\n\n'
        job.reply_closed = True

    class Tracked(StreamingResponse):
        def __init__(self, job):
            super().__init__(events(job), media_type='text/event-stream',
                headers={'X-Request-ID': job.request_id, 'Cache-Control': 'no-cache'})
            self.job = job
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                if not self.job.reply_closed and not self.job.finished.is_set():
                    service.spawn(service.cancel(self.job))

    @app.post('/v1/completions')
    async def completions(request: Request):
        rid = request.headers.get('X-Request-ID') or secrets.token_hex(16)
        job = await service.submit(await read_body(request), rid)
        await job.started.wait()
        if job.error:
            raise job.error
        if job.stream:
            return Tracked(job)
        await job.finished.wait()
        if job.error:
            raise job.error
        job.reply_closed = True
        return JSONResponse(service.completion(job), headers={'X-Request-ID': rid})

    @app.get('/health/live')
    async def live(): return {'alive': True}

    @app.get('/health/ready')
    async def ready():
        return JSONResponse({'ready': service.ready, 'reason': service.reason},
                            status_code=200 if service.ready else 503)

    @app.get('/admin/state')
    async def state(): return service.state()

    @app.get('/metrics')
    async def metrics(): return Response(service.metrics(), media_type='text/plain; version=0.0.4')

    return app
