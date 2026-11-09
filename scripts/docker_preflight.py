"""Read-only WSL Docker diagnostics; print proxy host/port but never credentials."""
import json
import os
import subprocess
from urllib.parse import urlsplit


def safe_proxy(value):
    if not value: return None
    parsed = urlsplit(value)
    return {'scheme': parsed.scheme, 'host': parsed.hostname, 'port': parsed.port,
            'hasCredentials': bool(parsed.username or parsed.password)}


def main():
    raw = subprocess.run(['docker', 'info', '--format', '{{json .}}'], capture_output=True, text=True, check=True)
    info = json.loads(raw.stdout)
    print(json.dumps({'shellProxy': {k: safe_proxy(os.environ.get(k))
          for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')},
          'daemonProxy': {k: safe_proxy(info.get(k)) for k in ('HttpProxy', 'HttpsProxy')},
          'dockerRoot': info.get('DockerRootDir'), 'containersRunning': info.get('ContainersRunning'),
          'serverVersion': info.get('ServerVersion')}))


if __name__ == '__main__': main()
