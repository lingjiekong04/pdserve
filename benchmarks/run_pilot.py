import argparse,json,subprocess,sys,time
from pathlib import Path
from make_trace import build,save
from campaign import ensure


def command(args):
    result=subprocess.run(args)
    if result.returncode:raise SystemExit(result.returncode)


def validate_stream_measurement(value, expected=10):
    all_group=value['groups']['all'];rows=value.get('requests',[])
    if (all_group['completed']!=expected or all_group['failed'] or all_group['rejected']
            or len(rows)!=expected or any(not row.get('success') for row in rows)):
        raise ValueError('Pilot requests did not all complete successfully')
    coverage=all_group.get('itl_single_token_coverage')
    # Socket reads can coalesce several cumulative SSE frames. ITL is valid only
    # for observed +1 token increments; require each stream to contribute such
    # samples and report coverage instead of inventing timestamps or imposing an
    # arbitrary coverage percentage.
    if (coverage is None or not 0 < coverage <= 1
            or all_group.get('itl_s',{}).get('count',0)<=0
            or any(row.get('single_token_intervals',0)<=0 for row in rows)):
        raise ValueError('Pilot has no honest single-token ITL sample for every stream')
    return True


def validate_pilot_summary(value, topology):
    if value.get('schema')!='pdserve.measurement.summary.v1' or value.get('topology')!=topology:
        raise ValueError('Pilot summary topology or schema mismatch')
    validate_stream_measurement(value)
    all_group=value['groups']['all']
    phase_counts={name:all_group[name]['count'] for name in ('prefill_s','transfer_s','decode_s')}
    if topology.startswith('pd_') and any(count!=10 for count in phase_counts.values()):
        raise ValueError('P/D native phase evidence incomplete: '+str(phase_counts))
    if topology=='mixed_fifo' and any(phase_counts.values()):
        raise ValueError('Mixed baseline must not fabricate P/D phases')
    return phase_counts


def main():
    parser=argparse.ArgumentParser();parser.add_argument('topology',choices=('mixed_fifo','pd_fifo','pd_fair'))
    parser.add_argument('--campaign',required=True);args=parser.parse_args();campaign_root,_=ensure(args.campaign)
    command([sys.executable,'tests/test_metrics.py']);command([sys.executable,'-u','scripts/preflight.py',args.topology])
    trace=Path('traces/stage3d-pilot.json');save(trace,build(10,80,.25,2031))
    folder=campaign_root/'pilots'/('stage3d-pilot-'+args.topology+'-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()));folder.mkdir(parents=True)
    raw=folder/'raw.json';summary=folder/'summary.json'
    command([sys.executable,'-u','benchmarks/client.py','--trace',str(trace),'--campaign',args.campaign,'--topology',args.topology,
        '--repeat','1','--request-prefix','s3d-pilot-'+args.topology,'--output',str(raw)])
    command([sys.executable,'-u','benchmarks/analyze.py','--raw',str(raw),'--output',str(summary)])
    value=json.loads(summary.read_text());all_group=value['groups']['all']
    validate_pilot_summary(value,args.topology)
    print('ITL single-token coverage: '
          f'{all_group["itl_single_token_coverage"]:.6f}; '
          f'samples={all_group["itl_s"]["count"]}; '
          f'batched_increments={all_group["batched_token_increments"]}',flush=True)
    print('STAGE3D_MEASUREMENT_PILOT_PASSED topology='+args.topology,flush=True)
    print('Summary: '+str(summary),flush=True)


if __name__=='__main__':main()
