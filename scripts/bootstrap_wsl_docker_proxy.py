"""One-time, reviewed Docker daemon proxy setup; never used by production tools.

Only a credential-free loopback proxy is accepted. Refuses to restart a daemon
with running containers or replace an existing different drop-in.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit


def info():
    return json.loads(subprocess.run(['docker', 'info', '--format', '{{json .}}'],
                      capture_output=True, text=True, check=True).stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (url.scheme not in ('http', 'https') or url.hostname != '127.0.0.1'
            or not url.port or url.username or url.password or url.path or url.query or url.fragment):
        raise SystemExit('Only a credential-free 127.0.0.1 proxy URL is supported')
    if os.geteuid() != 0: raise SystemExit('Deployment requires administrator authorization')
    before = info()
    if before['ContainersRunning']: raise SystemExit('Refusing to restart Docker with running containers')
    for key in ('HttpProxy', 'HttpsProxy'):
        if before.get(key) and before[key] != args.url:
            raise SystemExit('Existing different daemon proxy requires operator reconciliation')
    path = Path('/etc/systemd/system/docker.service.d/literature-proxy.conf')
    body = ('[Service]\nEnvironment="HTTP_PROXY=' + args.url + '"\n'
            'Environment="HTTPS_PROXY=' + args.url + '"\n'
            'Environment="NO_PROXY=localhost,127.0.0.1,::1"\n')
    if path.exists() and path.read_text() != body:
        raise SystemExit('Existing different drop-in preserved; no change made')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'restart', 'docker'], check=True)
    after = info()
    if after.get('HttpsProxy') != args.url: raise SystemExit('Daemon proxy verification failed')
    print(json.dumps({'dropIn': str(path), 'proxy': args.url, 'verified': True}))


if __name__ == '__main__': main()
