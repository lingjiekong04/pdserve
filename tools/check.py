"""Offline CPU checks only. No services, model downloads or inference."""
import ast,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    env=dict(os.environ,PYTHONPATH=os.pathsep.join(str(ROOT/p) for p in ('src','benchmarks','tests','scripts','engine_modules','tools')),
             PYTHONDONTWRITEBYTECODE='1')
    count=0
    for path in ROOT.rglob('*.py'):
        if any(p.startswith('.venv') or p in ('work','__pycache__') for p in path.relative_to(ROOT).parts):continue
        ast.parse(path.read_bytes(),filename=str(path),feature_version=(3,10));count+=1
    print('PYTHON310_SYNTAX_OK files='+str(count),flush=True)
    result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_*.py','-v'],cwd=ROOT,env=env)
    if result.returncode:return result.returncode
    print('OFFLINE_CPU_CHECKS_PASSED; no GPU, model or network request executed')
    return 0
if __name__=='__main__':raise SystemExit(main())
