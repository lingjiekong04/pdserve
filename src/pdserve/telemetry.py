"""Bounded-cardinality Prometheus exposition and request event JSONL."""
from collections import Counter, deque
import json
from pathlib import Path
import time

BUCKETS = (.001, .005, .01, .05, .1, .5, 1, 5, 30, 120, 600)


class Telemetry:
    def __init__(self, path=None):
        self.counts = Counter()
        self.rejections = Counter()
        self.output_tokens = 0
        self.histograms = {}
        self.events = deque(maxlen=4096)
        self.error = None
        self.handle = None
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.handle = Path(path).open('a', encoding='utf-8', buffering=1)

    def emit(self, event, **fields):
        row = {'event': event, 'monotonic_ns': time.monotonic_ns(), 'unix_ns': time.time_ns(), **fields}
        self.events.append(row)
        if self.handle:
            try:
                self.handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            except OSError as error:
                self.error = str(error)

    def observe(self, name, category, value):
        row = self.histograms.setdefault((name, category), [0, 0., [0] * len(BUCKETS)])
        row[0] += 1
        row[1] += value
        for index, bound in enumerate(BUCKETS):
            if value <= bound:
                row[2][index] += 1

    def render(self, service):
        lines = ['# TYPE pdserve_requests_total counter']
        for (status, category), count in sorted(self.counts.items()):
            lines.append(f'pdserve_requests_total{{status="{status}",class="{category}"}} {count}')
        lines += ['# TYPE pdserve_rejected_total counter']
        for code, count in sorted(self.rejections.items()):
            lines.append(f'pdserve_rejected_total{{code="{code}"}} {count}')
        lines += ['# TYPE pdserve_observed_output_tokens_total counter',
                  f'pdserve_observed_output_tokens_total {self.output_tokens}']
        declared = set()
        for (name, category), (count, total, buckets) in sorted(self.histograms.items()):
            metric = 'pdserve_' + name + '_seconds'
            if metric not in declared:
                lines.append(f'# TYPE {metric} histogram')
                declared.add(metric)
            for bound, value in zip(BUCKETS, buckets):
                lines.append(f'{metric}_bucket{{class="{category}",le="{bound}"}} {value}')
            lines += [f'{metric}_bucket{{class="{category}",le="+Inf"}} {count}',
                      f'{metric}_count{{class="{category}"}} {count}',
                      f'{metric}_sum{{class="{category}"}} {total}']
        lines += ['# TYPE pdserve_ready gauge', f'pdserve_ready {int(service.ready)}',
                  '# TYPE pdserve_queue_requests gauge']
        for kind, queue in service.queue.queues.items():
            lines.append(f'pdserve_queue_requests{{class="{kind}"}} {len(queue)}')
        if service.ledger:
            for role, state in service.ledger.snapshot().items():
                for key in ('reserved_tokens', 'reserved_slots', 'token_limit', 'slot_limit'):
                    lines.append(f'pdserve_gateway_{key}{{role="{role}"}} {state[key]}')
                observed = service.latest.get(role, {}).get('resources', {})
                for key in ('kv_free_tokens', 'kv_used_tokens', 'request_free_slots', 'metadata_free_slots'):
                    if key in observed:
                        lines.append(f'pdserve_engine_{key}{{role="{role}"}} {observed[key]}')
        return '\n'.join(lines) + '\n'

    def close(self):
        if self.handle:
            self.handle.close()
