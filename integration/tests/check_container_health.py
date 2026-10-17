"""Wait for the isolated container stack; health is not the full contract test."""
import json
from pathlib import Path
import sys
import time
import urllib.request

values=dict(line.split('=',1) for line in Path(sys.argv[1]).read_text().splitlines())
checks=[('http://127.0.0.1:18326/readyz',None),('http://127.0.0.1:18327/health',None),
        ('http://127.0.0.1:18328/readyz',values['AI_SERVICE_TOKEN'])]
for url,token in checks:
    deadline=time.monotonic()+180
    while True:
        try:
            headers={'Authorization':'Bearer '+token} if token else {}
            with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=3) as response:
                body=json.load(response)
            if url.endswith('/health'):
                assert body['catalog']['active'] and body['store']['connected']
            break
        except Exception:
            if time.monotonic()>deadline:raise
            time.sleep(1)
print('Container readiness confirmed; detailed behavior is covered by the separate contract job')
