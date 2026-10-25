"""Print derived public group statistics, not private raw measurements."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    value=json.loads((ROOT/'results/aggregates.json').read_text(encoding='utf-8'))
    for row in value['rows']:
        g=row['groups']['all']
        print(f"{row['scenario']:20} {row['topology']:12} n={g['requests']} "
              f"req/s={row['completed_throughput_rps']:.4f} TTFT_p95_ms={g['ttft_s']['p95']*1000:.2f}")
    print('Derived historical summary only. No causal scheduling gain or GPU replay claimed.')
if __name__=='__main__':main()
