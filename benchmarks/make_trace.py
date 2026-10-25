"""Create deterministic open-loop mixed-length workload traces."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random


def build(count, short_percent, rate, seed):
    short_count = count * short_percent
    if short_count % 100:
        raise ValueError('count * short-percent must be divisible by 100')
    short_count //= 100
    rng = random.Random(seed)
    classes = ['short'] * short_count + ['long'] * (count - short_count)
    rng.shuffle(classes)
    outputs = {kind: [128 if i % 2 == 0 else 512 for i in range(classes.count(kind))]
               for kind in ('short', 'long')}
    for values in outputs.values():
        rng.shuffle(values)
    offsets = []
    elapsed = 0.0
    for index in range(count):
        if index:
            elapsed += rng.expovariate(rate)
        offsets.append(round(elapsed, 9))
    requests = []
    positions = {'short': 0, 'long': 0}
    for index, (kind, offset) in enumerate(zip(classes, offsets)):
        output = outputs[kind][positions[kind]]
        positions[kind] += 1
        requests.append({'index': index, 'class': kind,
                         'input_tokens': 512 if kind == 'short' else 8192,
                         'output_tokens': output, 'scheduled_offset_s': offset})
    return {'schema': 'pdserve.openloop.v1', 'seed': seed, 'count': count,
            'short_percent': short_percent, 'arrival_rate_rps': rate,
            'prompt_pattern': "' hello' repeated input_tokens times",
            'requests': requests}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--count', type=int, default=10)
    parser.add_argument('--short-percent', type=int, choices=(20, 80), default=80)
    parser.add_argument('--rate', type=float, default=.25)
    parser.add_argument('--seed', type=int, default=2026)
    args = parser.parse_args()
    if args.count < 2 or not math.isfinite(args.rate) or args.rate <= 0:
        raise ValueError('count >= 2 and a positive finite rate are required')
    value = build(args.count, args.short_percent, args.rate, args.seed)
    digest = save(args.output, value)
    print(f'TRACE_SAVED path={args.output} count={args.count} sha256={digest}', flush=True)


if __name__ == '__main__':
    main()
