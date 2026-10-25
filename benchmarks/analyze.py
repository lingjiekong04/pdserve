"""Analyze client-observed streaming latency without inventing token timestamps."""
import argparse
import json
import math
from pathlib import Path


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return values[low]
    return values[low] * (high - position) + values[high] * (position - low)


def stats(values):
    return {'count': len(values), 'p50': percentile(values, .50),
            'p95': percentile(values, .95), 'p99': percentile(values, .99)}


def request_metrics(row, gateway):
    good = [f for f in row.get('frames', []) if 'completion_tokens' in f]
    counts = [f['completion_tokens'] for f in good]
    success = (row.get('status') == 200 and row.get('done') and not row.get('error')
               and not any('error' in f for f in row.get('frames', []))
               and counts and counts[-1] == row['output_tokens'])
    first = next((f for f in good if f['completion_tokens'] > 0), None)
    intervals = []
    eligible = 0
    batched = 0
    if first:
        previous = first
        previous_count = first['completion_tokens']
        for current in good[good.index(first) + 1:]:
            delta = current['completion_tokens'] - previous_count
            if delta == 1:
                intervals.append((current['arrival_ns'] - previous['arrival_ns']) / 1e9)
                eligible += 1
            elif delta > 1:
                batched += delta
            if delta > 0:
                previous = current
                previous_count = current['completion_tokens']
    queued = [e for e in gateway if e.get('request_id') == row['request_id'] and e.get('event') == 'queued']
    dispatched = [e for e in gateway if e.get('request_id') == row['request_id'] and e.get('event') == 'dispatched']
    queue_s = None
    if len(queued) == len(dispatched) == 1:
        queue_s = (dispatched[0]['monotonic_ns'] - queued[0]['monotonic_ns']) / 1e9
    own_engine = [e['native'] for e in gateway if e.get('request_id') == row['request_id']
                  and e.get('event') == 'engine_event' and isinstance(e.get('native'), dict)]
    def phase(role, start, end):
        starts = [e['monotonic_ns'] for e in own_engine if e.get('role') == role and e.get('stage') == start]
        ends = [e['monotonic_ns'] for e in own_engine if e.get('role') == role and e.get('stage') == end]
        if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
            return None
        return (ends[0] - starts[0]) / 1e9
    return {'request_id': row['request_id'], 'class': row['class'],
            'input_tokens': row['input_tokens'], 'output_tokens': row['output_tokens'],
            'success': success, 'status': row.get('status'), 'error': row.get('error'),
            'client_lateness_s': row['client_lateness_s'], 'queue_s': queue_s,
            'ttft_s': (first['arrival_ns'] - row['sent_ns']) / 1e9 if first else None,
            'scheduled_ttft_s': (first['arrival_ns'] - row['scheduled_ns']) / 1e9 if first else None,
            'e2e_s': (row['ended_ns'] - row['sent_ns']) / 1e9,
            'itl_s': intervals, 'single_token_intervals': eligible,
            'batched_token_increments': batched,
            'prefill_s': phase('prefill', 'prefill_started', 'prefill_completed'),
            'transfer_s': phase('prefill', 'prefill_completed', 'transfer_completed'),
            'decode_kv_wait_s': phase('decode', 'kv_preallocated', 'kv_ready'),
            'decode_s': phase('decode', 'decode_started', 'terminal')}


def group(rows):
    successes = [r for r in rows if r['success']]
    intervals = [value for row in successes for value in row['itl_s']]
    denominator = sum(max(0, r['output_tokens'] - 1) for r in successes)
    return {'requests': len(rows), 'completed': len(successes),
            'failed': sum(not r['success'] and r['status'] != 429 for r in rows),
            'rejected': sum(r['status'] == 429 for r in rows),
            'failure_rate': (sum(not r['success'] and r['status'] != 429 for r in rows) / len(rows)
                             if rows else None),
            'rejection_rate': (sum(r['status'] == 429 for r in rows) / len(rows) if rows else None),
            'ttft_s': stats([r['ttft_s'] for r in successes if r['ttft_s'] is not None]),
            'itl_s': stats(intervals),
            'itl_single_token_coverage': len(intervals) / denominator if denominator else None,
            'batched_token_increments': sum(r['batched_token_increments'] for r in successes),
            'e2e_s': stats([r['e2e_s'] for r in successes]),
            'queue_s': stats([r['queue_s'] for r in successes if r['queue_s'] is not None]),
            'client_lateness_s': stats([r['client_lateness_s'] for r in rows]),
            'prefill_s': stats([r['prefill_s'] for r in successes if r['prefill_s'] is not None]),
            'transfer_s': stats([r['transfer_s'] for r in successes if r['transfer_s'] is not None]),
            'decode_kv_wait_s': stats([r['decode_kv_wait_s'] for r in successes if r['decode_kv_wait_s'] is not None]),
            'decode_s': stats([r['decode_s'] for r in successes if r['decode_s'] is not None])}


def analyze(raw):
    rows = [request_metrics(row, raw.get('gateway_events', [])) for row in raw['requests']]
    completed = [r for r in rows if r['success']]
    duration = (max((r['ended_ns'] for r in raw['requests']), default=raw['ended_ns'])
                - min((r['sent_ns'] for r in raw['requests']), default=raw['started_ns'])) / 1e9
    summary = {'schema': 'pdserve.measurement.summary.v1', 'campaign': raw.get('campaign'),
               'topology': raw['topology'],
               'repeat': raw['repeat'], 'trace_sha256': raw['trace_sha256'],
               'performance_claim': False, 'duration_s': duration,
               'completed_throughput_rps': len(completed) / duration if duration else None,
               'output_throughput_tokens_per_s': sum(r['output_tokens'] for r in completed) / duration if duration else None,
               'groups': {'all': group(rows),
                          'short': group([r for r in rows if r['class'] == 'short']),
                          'long': group([r for r in rows if r['class'] == 'long'])},
               'requests': rows}
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--raw', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    raw = json.loads(args.raw.read_text())
    summary = analyze(raw)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary['groups'], ensure_ascii=False, indent=2), flush=True)
    print('MEASUREMENT_SUMMARY_SAVED ' + str(args.output), flush=True)


if __name__ == '__main__':
    main()
