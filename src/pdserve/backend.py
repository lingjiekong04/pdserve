"""Reuse the fixed SGLang /generate and Mooncake bootstrap protocol."""
import asyncio
from .core import ROLES, ServeError
from .protocol import SSEParser, frame_json


class AioBackend:
    def __init__(self, config):
        self.config = config
        self.urls = {'prefill': 'http://127.0.0.1:30000', 'decode': 'http://127.0.0.1:30001'}
        self.session = None

    async def open(self):
        import aiohttp
        self.aiohttp = aiohttp
        self.session = aiohttp.ClientSession(trust_env=False, read_bufsize=1_048_576,
            connector=aiohttp.TCPConnector(limit=256, limit_per_host=128),
            timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=self.config.generation_timeout))

    async def close(self):
        await self.session.close()

    async def snapshot(self, role):
        timeout = self.aiohttp.ClientTimeout(total=self.config.snapshot_timeout)
        async with self.session.get(self.urls[role] + '/get_server_info', timeout=timeout) as response:
            response.raise_for_status()
            info = await response.json()
        if len(info.get('internal_states', [])) != 1:
            raise ServeError(503, 'Exactly one scheduler per backend is required')
        expected = {'tp_size': 1, 'pp_size': 1, 'dp_size': 1, 'context_length': self.config.context,
                    'dtype': 'bfloat16', 'disable_radix_cache': True, 'disaggregation_mode': role}
        for key, value in expected.items():
            if info.get(key) != value:
                raise ServeError(503, f'{role}: unexpected engine setting {key}={info.get(key)}')
        state = info['internal_states'][0].get('pdserve', {})
        if state.get('capacity', {}).get('kv_tokens') != info.get('max_total_num_tokens'):
            raise ServeError(503, 'Observed KV capacity differs from engine capacity')
        return state

    async def read(self, role, job):
        body = {'rid': job.engine_id, 'input_ids': job.input_ids, 'stream': True,
                'bootstrap_host': '127.0.0.1', 'bootstrap_port': 8998, 'bootstrap_room': job.room,
                'sampling_params': {'temperature': job.temperature, 'ignore_eos': job.ignore_eos,
                                    'max_new_tokens': 1 if role == 'prefill' else job.output_limit,
                                    'stream_interval': 1}}
        parser, done = SSEParser(), False
        async with self.session.post(self.urls[role] + '/generate', json=body) as response:
            response.raise_for_status()
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                raise ServeError(502, 'Native backend did not return SSE')
            async for raw in response.content:
                data = parser.line(raw)
                if data is None:
                    continue
                if data == '[DONE]':
                    done = True
                    break
                yield frame_json(data)
        if not done:
            raise ServeError(502, 'Native stream ended without DONE')

    async def cancel(self, role, engine_id):
        timeout = self.aiohttp.ClientTimeout(total=5)
        async with self.session.post(self.urls[role] + '/pdserve/cancel', json={'rid': engine_id}, timeout=timeout) as response:
            receipt = await response.json()
            if (response.status != 202 or receipt.get('rid') != engine_id
                    or not receipt.get('cancel_enqueued') or receipt.get('cleanup_confirmed') is not False):
                raise ServeError(502, 'Invalid native cooperative cancellation receipt')
            return receipt
