import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def verify():
    value=json.loads((ROOT/'provenance/public-manifest.json').read_text())
    for row in value['files']:
        path=ROOT/row['path']
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise ValueError('Public artifact changed: '+row['path'])
    print('PUBLIC_MANIFEST_VERIFIED')
