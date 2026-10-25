"""Aggregate raw request-level metrics across three repeats without percentile-of-percentiles."""
import argparse,json
from pathlib import Path
from analyze import group


def aggregate(campaign,topology,scenario,summaries):
    rows=[row for summary in summaries for row in summary['requests']]
    duration=sum(summary['duration_s'] for summary in summaries)
    completed=[row for row in rows if row['success']]
    return {'schema':'pdserve.measurement.aggregate.v1','campaign':campaign,
        'topology':topology,'scenario':scenario,
        'repeats':len(summaries),'trace_sha256':summaries[0]['trace_sha256'],
        'duration_sum_s':duration,
        'completed_throughput_rps':len(completed)/duration if duration else None,
        'output_throughput_tokens_per_s':sum(r['output_tokens'] for r in completed)/duration if duration else None,
        'groups':{'all':group(rows),'short':group([r for r in rows if r['class']=='short']),
                  'long':group([r for r in rows if r['class']=='long'])},
        'source_repeats':[{'repeat':s['repeat'],'duration_s':s['duration_s']} for s in summaries]}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--topology',required=True)
    parser.add_argument('--campaign',required=True)
    parser.add_argument('--scenario',required=True);parser.add_argument('--inputs',nargs=3,required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path);args=parser.parse_args()
    summaries=[json.loads(path.read_text()) for path in args.inputs]
    if any(s.get('campaign')!=args.campaign or s.get('topology')!=args.topology
           or s.get('trace_sha256')!=summaries[0].get('trace_sha256') for s in summaries):
        raise ValueError('Repeat campaign, topology or trace mismatch')
    if sorted(s['repeat'] for s in summaries)!=[1,2,3]:raise ValueError('Exactly repeats 1,2,3 required')
    value=aggregate(args.campaign,args.topology,args.scenario,summaries);args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print('AGGREGATE_SAVED '+str(args.output),flush=True)


if __name__=='__main__':main()
