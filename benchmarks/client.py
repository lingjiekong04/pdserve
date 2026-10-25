"""Open-loop streaming client with true client-observed TTFT and frame timing."""
import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import time

from sse import Parser, frame


def safe_gateway_log(value):
    hint = value.get('gateway_event_log')
    if not hint:
        return None
    path = Path(hint)
    roots = (Path('logs/gateway').resolve(), Path('logs/baseline').resolve())
    if (path.is_symlink() or not path.is_file()
            or not any(path.resolve().is_relative_to(root) for root in roots)
            or path.name[:7] != 'events-' or path.suffix != '.jsonl'):
        raise ValueError('Unsafe gateway event-log hint')
    return path


async def json_get(session, url):
    async with session.get(url) as response:
        value = await response.json()
        return response.status, value


def idle(value):
    if not value.get('ready') or value.get('queue') != 0:
        return False
    if any(node['reserved_tokens'] or node['reserved_slots'] for node in value['budget'].values()):
        return False
    for node in value.get('engines', {}).values():
        resources = node.get('resources', {})
        if (resources.get('kv_used_tokens') != 0 or resources.get('kv_evictable_tokens', 0) != 0
                or any(resources.get(name) for name in
                       ('transport_status_entries', 'transport_info_entries', 'transport_queued_chunks'))
                or any(resources.get('queues', {}).values())):
            return False
    return True


def compact_state(value):
    return {'ready': value.get('ready'), 'reason': value.get('reason'),
            'queue': value.get('queue'),
            'budget': {role: {'reserved_tokens': row.get('reserved_tokens'),
                              'reserved_slots': row.get('reserved_slots')}
                       for role, row in value.get('budget', {}).items()},
            'resources': {role: {'kv_used_tokens': row.get('resources', {}).get('kv_used_tokens'),
                                 'kv_evictable_tokens': row.get('resources', {}).get('kv_evictable_tokens'),
                                 'queues': row.get('resources', {}).get('queues'),
                                 'transport_status_entries': row.get('resources', {}).get('transport_status_entries'),
                                 'transport_info_entries': row.get('resources', {}).get('transport_info_entries'),
                                 'transport_queued_chunks': row.get('resources', {}).get('transport_queued_chunks')}
                          for role, row in value.get('engines', {}).items()}}


class DrainError(RuntimeError):
    def __init__(self, message, state):
        super().__init__(message)
        self.state = state


async def wait_idle(session, endpoint, seconds=300):
    started = time.monotonic()
    deadline = time.monotonic() + seconds
    last = 0
    value = {}
    while time.monotonic() < deadline:
        status, value = await json_get(session, endpoint + '/admin/state')
        if status == 200 and value.get('ready') is False:
            detail = value.get('reason') or 'missing quarantine reason'
            raise DrainError('Gateway quarantined while draining: ' + detail, value)
        if status == 200 and idle(value):
            return status, value
        if time.monotonic() - last >= 5:
            elapsed = time.monotonic() - started
            print('[client] waiting for gateway/native drain '
                  f'elapsed={elapsed:.1f}s state='
                  + json.dumps(compact_state(value), ensure_ascii=False, separators=(',', ':')),
                  flush=True)
            last = time.monotonic()
        await asyncio.sleep(.25)
    raise DrainError(f'Gateway/native resources did not become fully idle after {seconds}s', value)


class Run:
    def __init__(self, endpoint, trace, topology, repeat, request_prefix, idle_timeout=300,
                 campaign=None):
        self.endpoint = endpoint.rstrip('/')
        self.trace = trace
        self.topology = topology
        self.repeat = repeat
        self.request_prefix = request_prefix
        self.idle_timeout = idle_timeout
        self.campaign = campaign
        self.done = 0
        self.started_ns = 0
        self.failure = None

    async def request(self, session, spec):
        target_ns = self.started_ns + round(spec['scheduled_offset_s'] * 1e9)
        delay = (target_ns - time.monotonic_ns()) / 1e9
        if delay > 0:
            await asyncio.sleep(delay)
        sent_ns = time.monotonic_ns()
        rid = f'{self.request_prefix}-{self.repeat}-{spec["index"]:04d}'
        body = {'model': 'pdserve-qwen', 'prompt': ' hello' * spec['input_tokens'],
                'max_tokens': spec['output_tokens'], 'ignore_eos': True,
                'temperature': 0, 'stream': True}
        row = dict(spec, request_id=rid, scheduled_ns=target_ns, sent_ns=sent_ns,
                   client_lateness_s=max(0, sent_ns - target_ns) / 1e9,
                   frames=[], done=False, error=None)
        try:
            timeout = self.aiohttp.ClientTimeout(total=420, connect=5, sock_read=360)
            async with session.post(self.endpoint + '/v1/completions', json=body,
                                    headers={'X-Request-ID': rid}, timeout=timeout) as response:
                row['status'] = response.status
                row['headers_ns'] = time.monotonic_ns()
                if response.status != 200:
                    row['response'] = await response.json()
                    return row
                if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                    raise ValueError('Expected text/event-stream')
                parser = Parser()
                while True:
                    raw = await response.content.readline()
                    if not raw:
                        break
                    arrival = time.monotonic_ns()
                    data = parser.line(raw)
                    if data is None:
                        continue
                    if data == '[DONE]':
                        row['done'] = True
                        row['done_ns'] = arrival
                        break
                    value = frame(data)
                    if value.get('id') not in (None, rid):
                        raise ValueError('SSE request ID mismatch')
                    usage = value.get('usage') or {}
                    count = usage.get('completion_tokens')
                    if 'error' in value:
                        row['frames'].append({'arrival_ns': arrival, 'error': value['error']})
                    elif type(count) is int:
                        row['frames'].append({'arrival_ns': arrival, 'completion_tokens': count,
                                              'usage': usage, 'finish_reason':
                                              (value.get('choices') or [{}])[0].get('finish_reason')})
        except Exception as error:
            row['error'] = repr(error)
        finally:
            row['ended_ns'] = time.monotonic_ns()
            self.done += 1
            print(f'[client] completed {self.done}/{self.trace["count"]} rid={rid} status={row.get("status")} error={row.get("error")}', flush=True)
        return row

    async def execute(self):
        import aiohttp
        self.aiohttp = aiohttp
        connector = aiohttp.TCPConnector(limit=256, limit_per_host=256)
        async with aiohttp.ClientSession(trust_env=False, connector=connector) as session:
            before_status, before = await json_get(session, self.endpoint + '/admin/state')
            if before_status != 200 or not before.get('ready'):
                raise ValueError('Gateway is not ready before measurement')
            log = safe_gateway_log(before)
            metrics_before_status, metrics_before = await self.metrics(session)
            self.started_ns = time.monotonic_ns() + 250_000_000
            rows = await asyncio.gather(*(self.request(session, spec) for spec in self.trace['requests']))
            ended_ns = time.monotonic_ns()
            try:
                after_status, after = await wait_idle(session, self.endpoint, self.idle_timeout)
            except Exception as error:
                state = error.state if isinstance(error, DrainError) else None
                self.failure = {'schema': 'pdserve.measurement.failure.v1',
                    'campaign': self.campaign,
                    'topology': self.topology, 'repeat': self.repeat,
                    'endpoint': self.endpoint, 'trace': self.trace,
                    'started_ns': self.started_ns, 'ended_ns': ended_ns,
                    'before_status': before_status, 'before': before,
                    'error_type': type(error).__name__, 'error': str(error) or repr(error),
                    'drain_state': state, 'drain_state_compact': compact_state(state or {}),
                    'requests': rows}
                raise
            metrics_after_status, metrics_after = await self.metrics(session)
        ids = {row['request_id'] for row in rows}
        events = []
        if log:
            for raw in log.read_bytes().splitlines(keepends=True):
                if not raw.endswith(b'\n'):
                    break
                item = json.loads(raw)
                if item.get('request_id') in ids:
                    events.append(item)
        return {'schema': 'pdserve.measurement.raw.v1', 'campaign': self.campaign,
                'topology': self.topology,
                'repeat': self.repeat, 'endpoint': self.endpoint,
                'trace': self.trace, 'started_ns': self.started_ns, 'ended_ns': ended_ns,
                'before_status': before_status, 'before': before,
                'after_status': after_status, 'after': after,
                'metrics_before_status': metrics_before_status, 'metrics_before': metrics_before,
                'metrics_after_status': metrics_after_status, 'metrics_after': metrics_after,
                'gateway_event_log': str(log) if log else None,
                'gateway_events': events, 'requests': rows}

    async def metrics(self, session):
        async with session.get(self.endpoint + '/metrics') as response:
            return response.status, await response.text()


def load_trace(path):
    raw = path.read_bytes()
    value = json.loads(raw)
    if (value.get('schema') != 'pdserve.openloop.v1' or value.get('count') != len(value.get('requests', []))
            or not value['requests'] or any(r['input_tokens'] not in (512, 8192)
                or r['output_tokens'] not in (128, 512) for r in value['requests'])):
        raise ValueError('Invalid fixed workload trace')
    return value, hashlib.sha256(raw).hexdigest()


def failure_path(output, stamp):
    return output.with_name(output.stem + '.failed-' + stamp + '.json')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--endpoint', default='http://127.0.0.1:8000')
    parser.add_argument('--trace', required=True, type=Path)
    parser.add_argument('--topology', required=True)
    parser.add_argument('--campaign', required=True)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--request-prefix', default='p3a')
    parser.add_argument('--idle-timeout', type=int, default=300)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    trace, digest = load_trace(args.trace)
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,48}', args.request_prefix):
        raise ValueError('request-prefix must be 1..48 safe ASCII characters')
    if not 30 <= args.idle_timeout <= 900:
        raise ValueError('idle-timeout must be between 30 and 900 seconds')
    from campaign import valid_name
    valid_name(args.campaign)
    runner = Run(args.endpoint, trace, args.topology, args.repeat,
                 args.request_prefix, args.idle_timeout,args.campaign)
    try:
        value = asyncio.run(runner.execute())
    except Exception:
        if runner.failure:
            runner.failure['trace_path'] = str(args.trace)
            runner.failure['trace_sha256'] = digest
            path = failure_path(args.output, time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
            atomic_json(path, runner.failure)
            print('FAILED_MEASUREMENT_DIAGNOSTIC_SAVED ' + str(path), flush=True)
        raise
    value['trace_path'] = str(args.trace)
    value['trace_sha256'] = digest
    atomic_json(args.output, value)
    print('RAW_MEASUREMENT_SAVED ' + str(args.output), flush=True)


if __name__ == '__main__':
    main()
