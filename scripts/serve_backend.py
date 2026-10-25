"""Explicit user-run SGLang launcher; --dry-run never imports Torch."""
import argparse,json,os,socket,subprocess,sys
from pathlib import Path

def configuration(role,model,ib_device='mlx5_0'):
    if role not in ('prefill','decode','replica0','replica1'):raise ValueError('Unknown role')
    gpu=0 if role in ('prefill','replica0') else 1
    port=30000+gpu
    cmd=[sys.executable,'-u','-m','sglang.launch_server','--model-path',str(model),
        '--served-model-name','pdserve-qwen','--host','127.0.0.1','--port',str(port),
        '--tp-size','1','--dtype','bfloat16','--context-length','16384',
        '--attention-backend','flashinfer','--mem-fraction-static','0.80',
        '--max-running-requests','32','--cuda-graph-max-bs','32',
        '--random-seed','2026','--disable-radix-cache','--enable-metrics']
    env={'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','CUDA_VISIBLE_DEVICES':str(gpu)}
    if role in ('prefill','decode'):
        env.update(CUDA_VISIBLE_DEVICES='0,1',PDSERVE_ENGINE_OBSERVER='1',
            PDSERVE_ENGINE_LIFECYCLE='1',PDSERVE_ENGINE_CANCEL='1',PDSERVE_ENGINE_FAULT_GUARD='1',
            PDSERVE_TRANSFER_TIMEOUT_S='30',PDSERVE_KV_READY_TIMEOUT_S='60',PDSERVE_ENABLE_TEST_HOOKS='0')
        cmd+=['--base-gpu-id',str(gpu),'--disaggregation-mode',role,
            '--disaggregation-transfer-backend','mooncake','--disaggregation-bootstrap-port','8998',
            '--disaggregation-ib-device',ib_device]
    else:
        env.update(PDSERVE_ENGINE_OBSERVER='0',PDSERVE_ENGINE_LIFECYCLE='0',
            PDSERVE_ENGINE_CANCEL='0',PDSERVE_ENGINE_FAULT_GUARD='0',PDSERVE_ENABLE_TEST_HOOKS='0')
    return cmd,env,port

def main():
    p=argparse.ArgumentParser();p.add_argument('role',choices=('prefill','decode','replica0','replica1'))
    p.add_argument('--model-path',required=True,type=Path);p.add_argument('--ib-device',default='mlx5_0')
    p.add_argument('--dry-run',action='store_true');a=p.parse_args()
    cmd,extra,port=configuration(a.role,a.model_path,a.ib_device)
    if a.dry_run:print(json.dumps({'command':cmd,'environment':extra,'port':port},indent=2));return
    if sys.platform!='linux':raise SystemExit('Real SGLang/Mooncake execution is Linux-only')
    if not (a.model_path/'config.json').is_file():raise SystemExit('Local model config.json missing')
    for value in ([port,8998] if a.role=='prefill' else [port]):
        with socket.socket() as s:
            try:s.bind(('127.0.0.1',value))
            except OSError as e:raise SystemExit(f'Port {value} unavailable: {e}')
    os.execvpe(sys.executable,cmd,dict(os.environ,**extra))
if __name__=='__main__':main()
