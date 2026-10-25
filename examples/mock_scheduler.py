"""Synthetic queue/budget demo; not a GPU or model-serving demonstration."""
import sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from pdserve.core import Config,WaitQueue
from types import SimpleNamespace
class SyntheticBudget:
    def fits(self,job):return True
q=WaitQueue(Config());now=time.monotonic()
for i,kind in enumerate(['short']*6+['long']*2):
    q.add(SimpleNamespace(category=kind,sequence=i,enqueued_at=now))
while len(q):
    job=q.choose(SyntheticBudget(),now);q.remove(job);print(job.category,job.sequence)
print('SYNTHETIC_DEMO_ONLY: no GPU/model/backend involved')
