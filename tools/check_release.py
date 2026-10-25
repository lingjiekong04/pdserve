"""Offline scope, links, source provenance and full public-file hash checks."""
import hashlib,json,re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
IGNORED={'.git','__pycache__','.venv','.venv-gpu','work','reports','logs','models'}
def files():
    for p in ROOT.rglob('*'):
        rel=p.relative_to(ROOT)
        if any(part in IGNORED for part in rel.parts):continue
        if p.is_symlink():raise ValueError('Symlink in candidate: '+str(rel))
        if p.is_file():yield p

def main():
    path=ROOT/'provenance/public-manifest.json'
    m=json.loads(path.read_text(encoding='utf-8'));expected={x['path']:x['sha256'] for x in m['files']}
    actual={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files() if p!=path}
    if actual!=expected:
        missing=set(expected)-set(actual);extra=set(actual)-set(expected)
        changed=[n for n in set(expected)&set(actual) if expected[n]!=actual[n]]
        raise ValueError(f'Manifest differs: missing={missing}, extra={extra}, changed={changed}')
    broken=[]
    for p in ROOT.rglob('*.md'):
        if any(part in IGNORED for part in p.relative_to(ROOT).parts):continue
        text=p.read_text(encoding='utf-8')
        for target in re.findall(r'\]\(([^)]+)\)',text):
            if target.startswith(('https://','http://','#','mailto:')):continue
            target=target.split('#')[0]
            if target and not (p.parent/target).exists():broken.append((p.name,target))
    if broken:raise ValueError('Broken relative links: '+str(broken))
    for p in files():
        if p.suffix.lower() in ('.zip','.bundle','.safetensors','.bin','.pt','.pth','.pem','.key'):
            raise ValueError('Excluded artifact in public candidate: '+p.name)
    for row in json.loads((ROOT/'provenance/source-map.json').read_text(encoding='utf-8')):
        if row['kind']=='byte-identical' and row['candidate_sha256']!=row['source_sha256']:
            raise ValueError('Inherited file changed: '+row['candidate_path'])
    print(f'PUBLIC_RELEASE_CHECKS_PASSED files={len(actual)+1}; links and manifest valid')
    print('No GPU/installation/business/production validation is implied.')
if __name__=='__main__':main()
