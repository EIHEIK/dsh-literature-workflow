"""Run only inside the validation container; no API calls or personal data."""
import json
import os
from pathlib import Path
import socket

checks = {}
checks['nonRoot'] = os.geteuid() == 10001
try:
    Path('/etc/literature-probe').write_text('must be denied')
    checks['rootReadOnly'] = False
except OSError:
    checks['rootReadOnly'] = True
try:
    Path('/app/probe.py').write_text('must be denied')
    checks['inputReadOnly'] = False
except OSError:
    checks['inputReadOnly'] = True
checks['hostFilesNotMounted'] = not Path('/mnt/c/Users').exists() and not Path('/var/run/docker.sock').exists()
try:
    with socket.create_connection(('1.1.1.1', 443), timeout=2):
        checks['directNetworkDenied'] = False
except OSError:
    checks['directNetworkDenied'] = True
status = Path('/proc/self/status').read_text()
checks['capabilitiesDropped'] = 'CapEff:\t0000000000000000' in status
checks['noNewPrivileges'] = 'NoNewPrivs:\t1' in status
Path('/work/writable.txt').write_text('only the mounted validation output')
checks['jobOutputWritable'] = Path('/work/writable.txt').read_text().startswith('only')
report = {'passed': all(checks.values()), 'checks': checks,
          'boundary': 'Actual base container confinement; not production worker/API/Zotero integration'}
Path('/work/result.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report))
raise SystemExit(0 if report['passed'] else 1)
