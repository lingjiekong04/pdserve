"""Run or resume one topology's 2 mixes x 2 loads x 3 repeats matrix."""
import argparse,hashlib,json,subprocess,sys,time,uuid
from pathlib import Path
from campaign import ensure
from make_trace import build,save

SCENARIOS=(('20short-low',20,.25,2027),('20short-saturated',20,2.0,2028),
           ('80short-low',80,.25,2029),('80short-saturated',80,2.0,2030))


def command(args):
    result=subprocess.run(args)
    if result.returncode:raise SystemExit(result.returncode)


def ensure_trace(path,short,rate,seed):
    value=build(40,short,rate,seed)
    if path.exists():
        existing=json.loads(path.read_text())
        if existing!=value:raise ValueError('Existing fixed trace differs: '+str(path))
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return save(path,value)


def valid_done(raw_path,summary_path,campaign,topology,repeat,digest):
    if not raw_path.is_file() or not summary_path.is_file():return False
    try:raw=json.loads(raw_path.read_text());summary=json.loads(summary_path.read_text())
    except (ValueError,OSError):return False
    return (raw.get('campaign')==campaign and summary.get('campaign')==campaign
        and raw.get('topology')==topology and raw.get('repeat')==repeat
        and raw.get('trace_sha256')==digest and len(raw.get('requests',[]))==40
        and summary.get('schema')=='pdserve.measurement.summary.v1'
        and summary.get('topology')==topology and summary.get('repeat')==repeat)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('topology',choices=('mixed_fifo','pd_fifo','pd_fair'))
    parser.add_argument('--campaign',required=True);args=parser.parse_args()
    campaign_root,campaign=ensure(args.campaign)
    command([sys.executable,'-u','scripts/preflight.py',args.topology])
    root=campaign_root/args.topology;trace_root=Path('traces/matrix');trace_root.mkdir(parents=True,exist_ok=True)
    total=len(SCENARIOS)*3;position=0
    for scenario,short,rate,seed in SCENARIOS:
        trace=trace_root/(scenario+'.json');digest=ensure_trace(trace,short,rate,seed);summaries=[]
        for repeat in (1,2,3):
            position+=1;folder=root/scenario/f'repeat-{repeat}';raw=folder/'raw.json';summary=folder/'summary.json';folder.mkdir(parents=True,exist_ok=True)
            if valid_done(raw,summary,args.campaign,args.topology,repeat,digest):
                print(f'[run {position}/{total}] SKIP accepted {scenario} repeat={repeat}',flush=True);summaries.append(summary);continue
            prefix=f's3d-{short}-{"sat" if rate>1 else "low"}-r{repeat}-{uuid.uuid4().hex[:6]}'
            print(f'[run {position}/{total}] START {scenario} repeat={repeat} rate={rate} count=40',flush=True)
            command([sys.executable,'-u','benchmarks/client.py','--trace',str(trace),'--campaign',args.campaign,'--topology',args.topology,
                '--repeat',str(repeat),'--request-prefix',prefix,'--idle-timeout','300','--output',str(raw)])
            command([sys.executable,'-u','benchmarks/analyze.py','--raw',str(raw),'--output',str(summary)])
            if not valid_done(raw,summary,args.campaign,args.topology,repeat,digest):raise ValueError('Saved run failed integrity check')
            summaries.append(summary);print(f'[run {position}/{total}] DONE {scenario} repeat={repeat}',flush=True)
        command([sys.executable,'-u','benchmarks/aggregate.py','--campaign',args.campaign,'--topology',args.topology,
            '--scenario',scenario,'--inputs',*(str(p) for p in summaries),'--output',str(root/scenario/'aggregate.json')])
    manifest={'schema':'pdserve.stage3d.topology.v1','campaign':args.campaign,
        'hardware_sha256':campaign['hardware_sha256'],'topology':args.topology,
        'completed_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
        'scenarios':[s[0] for s in SCENARIOS],'repeats':3,'requests_per_run':40,'runs':total,
        'low_rate_rps':.25,'saturated_rate_rps':2.0}
    (root/'complete.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print('STAGE3D_TOPOLOGY_MATRIX_PASSED topology='+args.topology,flush=True)


if __name__=='__main__':main()
