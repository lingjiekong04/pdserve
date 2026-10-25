"""Benchmark gateway for two mixed replicas with a loaded control-plane timeout."""
import argparse
import asyncio
import fcntl
from pathlib import Path
import sys
import uuid

from pdserve.mixed import EventLog,FIFOService,NativeBackend,ServeError,build_app,parse_metrics
from pdserve.benchmark_config import MIXED_RESOURCE_POLL_S,SNAPSHOT_TIMEOUT_S

class BenchmarkBackend(NativeBackend):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.static={}

    async def metrics_snapshot(self,name,base):
        timeout=self.aiohttp.ClientTimeout(total=SNAPSHOT_TIMEOUT_S)
        try:
            async with self.session.get(self.urls[name]+'/metrics',timeout=timeout) as response:
                response.raise_for_status();metrics=parse_metrics(await response.text())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            detail=str(error) or repr(error)
            raise RuntimeError(f'{name} resource metrics failed: '
                               f'{type(error).__name__}: {detail}') from error
        capacity=base['capacity']['kv_tokens'];used=int(metrics.get('kv_used_tokens',0))
        return {**base,'resources':{'kv_used_tokens':used,'kv_free_tokens':capacity-used,
            'queues':{'running':int(metrics.get('running',0)),
                      'waiting':int(metrics.get('waiting',0))}}}

    async def snapshot(self,name):
        timeout=self.aiohttp.ClientTimeout(total=SNAPSHOT_TIMEOUT_S)
        try:
            async with self.session.get(self.urls[name]+'/get_server_info',timeout=timeout) as response:
                response.raise_for_status();info=await response.json()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            detail=str(error) or repr(error)
            raise RuntimeError(f'{name} control-plane snapshot failed: '
                               f'{type(error).__name__}: {detail}') from error
        expected={'tp_size':1,'pp_size':1,'dp_size':1,'context_length':16384,
            'dtype':'bfloat16','disable_radix_cache':True,
            'disaggregation_mode':'null','served_model_name':'pdserve-qwen'}
        for key,value in expected.items():
            if info.get(key)!=value:
                raise ServeError(503,f'{name}: unexpected engine setting {key}={info.get(key)}')
        capacity=info.get('max_total_num_tokens')
        if type(capacity) is not int or capacity<16384:
            raise ServeError(503,name+': invalid KV capacity')
        base={'healthy':True,'capacity':{'kv_tokens':capacity,'page_size':info.get('page_size')},
            'settings':{key:info.get(key) for key in expected}}
        self.static[name]=base
        return await self.metrics_snapshot(name,base)

    async def resource_snapshot(self,name):
        if name not in self.static:raise RuntimeError(name+': static identity was not initialized')
        return await self.metrics_snapshot(name,self.static[name])


class BenchmarkService(FIFOService):
    async def poll(self):
        # Static engine configuration was validated once during start. Poll only
        # exported resource gauges during a benchmark so observation does not
        # contend with SGLang's engine-control request path.
        values=await asyncio.gather(*(self.backend.resource_snapshot(name) for name in self.names))
        self.latest=dict(zip(self.names,values));self.dispatch()

    async def monitor(self):
        while not self.closing:
            try:await self.poll()
            except asyncio.CancelledError:raise
            except Exception as error:
                detail=str(error) or repr(error)
                self.ready,self.reason=False,f'{type(error).__name__}: {detail}'
                self.telemetry.emit('baseline_quarantined',reason=self.reason)
            await asyncio.sleep(MIXED_RESOURCE_POLL_S)

    def state(self):
        value=super().state()
        value['control_plane_snapshot_timeout_s']=SNAPSHOT_TIMEOUT_S
        value['resource_poll']='metrics_only_after_start'
        value['resource_poll_interval_s']=MIXED_RESOURCE_POLL_S
        return value


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model-path',required=True)
    parser.add_argument('--active-per-replica',type=int,default=32)
    parser.add_argument('--queue-limit',type=int,default=128)
    parser.add_argument('--queue-timeout',type=float,default=30)
    args=parser.parse_args()
    import uvicorn
    from transformers import AutoTokenizer
    Path('logs/baseline').mkdir(parents=True,exist_ok=True)
    lock=open('logs/baseline/gateway.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    tokenizer=AutoTokenizer.from_pretrained(args.model_path,local_files_only=True)
    async def encode(prompt):return await asyncio.to_thread(tokenizer.encode,prompt,add_special_tokens=False)
    path=Path('logs/baseline')/('events-'+uuid.uuid4().hex+'.jsonl')
    backend=BenchmarkBackend({'replica0':'http://127.0.0.1:30000',
                              'replica1':'http://127.0.0.1:30001'})
    service=BenchmarkService(backend,encode,EventLog(path),args.active_per_replica,
                             args.queue_limit,args.queue_timeout)
    print(f'Baseline event log: {path}; topology=two_mixed_replicas_fifo; '
          f'active_per_replica={args.active_per_replica}; queue_limit={args.queue_limit}; '
          f'control_plane_snapshot_timeout_s={SNAPSHOT_TIMEOUT_S}; '
          f'resource_poll=metrics_only_after_start',flush=True)
    uvicorn.run(build_app(service),host='127.0.0.1',port=8000,workers=1,log_level='info')


if __name__=='__main__':main()
