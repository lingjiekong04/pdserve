"""Rebuild into a NEW directory only; original upstream/environment never edited."""
import argparse,hashlib,json,shutil,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def rebuild(upstream,output):
    upstream=Path(upstream).resolve();output=Path(output).resolve()
    if output.exists():raise ValueError('Output must not exist')
    if output==upstream or output.is_relative_to(upstream):raise ValueError('Output must be outside upstream')
    manifest=json.loads((ROOT/'patches/manifest.json').read_text())
    for name,row in manifest['files'].items():
        p=upstream/name
        if p.is_symlink():raise ValueError('Symlink patch target')
        before=row['before_sha256']
        if (before is None and p.exists()) or (before is not None and (not p.is_file() or sha(p)!=before)):
            raise ValueError('Unexpected upstream bytes: '+name)
    if not (upstream/'sglang').is_dir():raise ValueError('Expected upstream/sglang directory')
    # Refuse symlinks anywhere in copied upstream code.
    if any(p.is_symlink() for p in (upstream/'sglang').rglob('*')):raise ValueError('Symlink in upstream')
    output.mkdir(parents=True)
    shutil.copytree(upstream/'sglang',output/'sglang',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    patch=ROOT/'patches/sglang-v0.4.6.post5-pdserve.patch'
    for flags in (['--check'],[]):
        result=subprocess.run(['git','-c','core.autocrlf=false','apply','--whitespace=nowarn',*flags,str(patch)],cwd=output,
                              capture_output=True,text=True)
        if result.returncode:raise ValueError(result.stdout+result.stderr)
    for name,row in manifest['files'].items():
        if sha(output/name)!=row['after_sha256']:raise ValueError('Patched hash mismatch: '+name)
    print('PATCH_REBUILD_PASSED files='+str(len(manifest['files'])))
    return output

def main():
    p=argparse.ArgumentParser();p.add_argument('--upstream',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path);a=p.parse_args();rebuild(a.upstream,a.output)
if __name__=='__main__':main()
