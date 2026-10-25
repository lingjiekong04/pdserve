"""Benchmark P/D gateway with an explicit FIFO or fair scheduling mode."""
import argparse
import asyncio
import fcntl
from pathlib import Path
import sys
import uuid

from pdserve.api import build_app
from pdserve.backend import AioBackend
from pdserve.core import Config
from pdserve.telemetry import Telemetry
from pdserve.context_guard import GuardedService
from pdserve.benchmark_config import SNAPSHOT_TIMEOUT_S


class ExperimentService(GuardedService):
    async def states(self):
        async def snapshot(role):
            try:
                return await self.backend.snapshot(role)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                detail=str(error) or repr(error)
                raise RuntimeError(f'{role} control-plane snapshot failed: '
                                   f'{type(error).__name__}: {detail}') from error
        values=await asyncio.gather(*(snapshot(role) for role in ('prefill','decode')))
        return dict(zip(('prefill','decode'),values))

    def state(self):
        value=super().state();mode=self.config.mode
        value.update(topology='prefill_decode_'+mode,
            scheduler='strict_fifo' if mode=='fifo' else 'weighted_3_to_1_with_5s_long_aging',
            active_limit=self.config.active_limit,queue_limit=self.config.queue_limit,
            control_plane_snapshot_timeout_s=self.config.snapshot_timeout)
        return value


def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('fifo','fair'))
    parser.add_argument('--model-path',required=True);parser.add_argument('--active-limit',type=int,default=32)
    parser.add_argument('--queue-limit',type=int,default=128);args=parser.parse_args()
    import uvicorn
    from transformers import AutoTokenizer
    Path('logs/gateway').mkdir(parents=True,exist_ok=True)
    lock=open('logs/gateway/gateway.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    tokenizer=AutoTokenizer.from_pretrained(args.model_path,local_files_only=True)
    async def encode(prompt):return await asyncio.to_thread(tokenizer.encode,prompt,add_special_tokens=False)
    path=Path('logs/gateway')/('events-'+uuid.uuid4().hex+'.jsonl')
    # The stock 3 s control-plane timeout is appropriate for acceptance tests,
    # but /get_server_info can legitimately wait behind saturated GPU work.
    # Thirty seconds still quarantines an unavailable node while avoiding a
    # false permanent quarantine caused by a transient loaded snapshot.
    config=Config(active_limit=args.active_limit,queue_limit=args.queue_limit,
                  mode=args.mode,snapshot_timeout=SNAPSHOT_TIMEOUT_S)
    service=ExperimentService(AioBackend(config),encode,config,Telemetry(path))
    print(f'Gateway event log: {path}; topology=prefill_decode_{args.mode}; '
          f'active_limit={args.active_limit}; queue_limit={args.queue_limit}',flush=True)
    uvicorn.run(build_app(service),host='127.0.0.1',port=8000,workers=1,log_level='info')


if __name__=='__main__':main()
