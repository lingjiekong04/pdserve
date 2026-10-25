import argparse,json
from pathlib import Path
from urllib.request import build_opener,ProxyHandler,Request
from candidate_manifest import verify


def get(url):
    with build_opener(ProxyHandler({})).open(Request(url),timeout=10) as response:return response.status,json.load(response)


def idle(state):
    if not state.get('ready') or state.get('queue'):return False
    if any(row['reserved_tokens'] or row['reserved_slots'] for row in state['budget'].values()):return False
    for engine in state['engines'].values():
        resources=engine.get('resources',{})
        if resources.get('kv_used_tokens') or resources.get('kv_evictable_tokens') or any(resources.get('queues',{}).values()):return False
        if any(resources.get(k) for k in ('transport_status_entries','transport_info_entries','transport_queued_chunks')):return False
    return True


def main():
    parser=argparse.ArgumentParser();parser.add_argument('topology',choices=('mixed_fifo','pd_fifo','pd_fair'));parser.add_argument('--model-path',default=__import__('os').environ.get('PDSERVE_MODEL_PATH'));args=parser.parse_args();verify()
    if not args.model_path:raise ValueError('Set PDSERVE_MODEL_PATH or --model-path')
    _,state=get('http://127.0.0.1:8000/admin/state')
    expected={'mixed_fifo':'two_mixed_replicas_fifo','pd_fifo':'prefill_decode_fifo','pd_fair':'prefill_decode_fair'}[args.topology]
    if state.get('topology')!=expected or not idle(state) or state.get('queue_limit')!=128:raise ValueError('Wrong or non-idle benchmark gateway')
    if args.topology=='mixed_fifo':
        if (state.get('active_per_replica')!=32 or state.get('scheduler')!='strict_fifo'
                or state.get('control_plane_snapshot_timeout_s')!=30
                or state.get('resource_poll')!='metrics_only_after_start'
                or state.get('resource_poll_interval_s')!=1):
            raise ValueError('Mixed baseline limits or observation policy changed')
        roles=(('replica0',30000,'null'),('replica1',30001,'null'))
    else:
        scheduler='strict_fifo' if args.topology=='pd_fifo' else 'weighted_3_to_1_with_5s_long_aging'
        if (state.get('active_limit')!=32 or state.get('scheduler')!=scheduler
                or state.get('control_plane_snapshot_timeout_s')!=30):
            raise ValueError('P/D benchmark limits or control-plane timeout changed')
        roles=(('prefill',30000,'prefill'),('decode',30001,'decode'))
    common={'tp_size':1,'pp_size':1,'dp_size':1,'context_length':16384,'dtype':'bfloat16',
            'disable_radix_cache':True,'served_model_name':'pdserve-qwen'}
    engines={}
    for name,port,mode in roles:
        code,info=get(f'http://127.0.0.1:{port}/get_server_info')
        if code!=200 or info.get('disaggregation_mode')!=mode or any(info.get(k)!=v for k,v in common.items()):raise ValueError(name+' settings changed')
        engines[name]={k:info.get(k) for k in (*common,'disaggregation_mode','max_total_num_tokens','page_size')}
    from transformers import AutoTokenizer
    tokenizer=AutoTokenizer.from_pretrained(args.model_path,local_files_only=True)
    if any(len(tokenizer.encode(' hello'*n,add_special_tokens=False))!=n for n in (512,8192)):raise ValueError('Token fixtures changed')
    target=Path('reports/benchmarks/preflight-'+args.topology+'.json');target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps({'passed':True,'topology':args.topology,'state':state,'engines':engines},indent=2)+'\n')
    print('STAGE3D_PREFLIGHT_PASSED topology='+args.topology,flush=True)


if __name__=='__main__':main()
