"""Bind a benchmark campaign to one immutable host and GPU pair."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import socket
import subprocess
import time

SCHEMA='pdserve.stage3d.campaign.v1'
ROOT=Path('reports/benchmarks')


def valid_name(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,47}',value):
        raise ValueError('Campaign must be 1..48 lowercase safe ASCII characters')
    if value in ('stage3d-v1','pilots'):
        raise ValueError('Campaign name is reserved for historical results')
    return value


def file_sha(path):
    path=Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def command(args):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=30).stdout.strip()


def hardware():
    query=command(['nvidia-smi','-i','0,1',
        '--query-gpu=index,uuid,name,memory.total,driver_version,pci.bus_id',
        '--format=csv,noheader,nounits'])
    rows=[]
    for line in query.splitlines():
        fields=[field.strip() for field in line.split(',')]
        if len(fields)!=6:raise ValueError('Unexpected nvidia-smi identity output')
        rows.append(dict(zip(('index','uuid','name','memory_mib','driver','pci_bus_id'),fields)))
    if [row['index'] for row in rows]!=['0','1'] or len({row['uuid'] for row in rows})!=2:
        raise ValueError('Exactly distinct physical GPU indices 0 and 1 are required')
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    return {'hostname':socket.gethostname(),'boot_id':boot,
        'kernel':platform.release(),'machine':platform.machine(),'gpus':rows,
        'topology':command(['nvidia-smi','topo','-m']),
        'model_manifest_sha256':file_sha('resources/model-files.json'),
        'requirements_lock_sha256':file_sha('requirements/historical-linux.lock'),
        'stage3d_source_manifest_sha256':file_sha('provenance/public-manifest.json')}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def ensure(name):
    name=valid_name(name);current=hardware();digest=fingerprint(current)
    folder=ROOT/name;path=folder/'campaign.json'
    if path.exists():
        saved=json.loads(path.read_text(encoding='utf-8'))
        if (saved.get('schema')!=SCHEMA or saved.get('campaign')!=name
                or saved.get('hardware')!=current or saved.get('hardware_sha256')!=digest):
            raise ValueError('Campaign hardware identity changed; choose a new campaign name')
    else:
        folder.mkdir(parents=True,exist_ok=True)
        saved={'schema':SCHEMA,'campaign':name,
            'created_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
            'hardware_sha256':digest,'hardware':current}
        temporary=path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(saved,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(path)
    return folder,saved


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--campaign',required=True);args=parser.parse_args()
    folder,value=ensure(args.campaign)
    print(json.dumps(value,ensure_ascii=False,indent=2),flush=True)
    print('STAGE3D_CAMPAIGN_READY path='+str(folder),flush=True)


if __name__=='__main__':main()
