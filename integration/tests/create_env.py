"""Generate untracked, synthetic-only local Compose access values."""
import argparse
import os
from pathlib import Path
import uuid

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('output',type=Path)
args=p.parse_args()
values={'AI_SERVICE_TOKEN':'synthetic-'+uuid.uuid4().hex,'SESSION_SECRET':'synthetic-'+uuid.uuid4().hex,
        'DEMO_ACCESS_TOKEN':'synthetic-'+uuid.uuid4().hex,'QA_ADMIN_PASSWORD':'synthetic-'+uuid.uuid4().hex,
        'INTEGRATION_RUN_ID':uuid.uuid4().hex[:8]}
args.output.parent.mkdir(parents=True,exist_ok=True)
fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as file:
    file.write(''.join(f'{name}={value}\n' for name,value in values.items()))
print('Created synthetic-only access configuration:',args.output)
