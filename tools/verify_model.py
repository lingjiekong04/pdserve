"""Check local model bytes only; never downloads or loads weights into a model."""
import argparse,hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('model_dir',type=Path);a=p.parse_args()
    manifest=json.loads((ROOT/'resources/model-files.json').read_text())
    for row in manifest['files']:
        path=a.model_dir/row['path']
        if path.is_symlink() or not path.is_file():raise ValueError('Missing/linked file: '+row['path'])
        h=hashlib.sha256()
        with path.open('rb') as f:
            for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
        if path.stat().st_size!=row['bytes'] or h.hexdigest()!=row['sha256']:
            raise ValueError('Local model mismatch: '+row['path'])
        print('PASS '+row['path'],flush=True)
    print('LOCAL_MODEL_BYTES_MATCH; upstream revision remains unverified')
if __name__=='__main__':main()
