"""Prepare writable engine directories, without changing the controller environment.

This is environment hygiene, NOT a filesystem or network sandbox. Production
confinement is supplied by the container runner, independently of these paths.
"""
import os
from pathlib import Path


def engine_environment(root, parent=None):
    parent = os.environ if parent is None else parent
    root = Path(root).resolve()
    directories = {name: root / name for name in ('home', 'config', 'cache', 'temp', 'appdata', 'localappdata')}
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)
        if not path.resolve().is_relative_to(root):
            raise ValueError('Engine environment directory escapes its root')
    # Allowlist instead of trying to enumerate every provider's secret variable.
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'SYSTEMDRIVE',
               'LANG', 'LC_ALL', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE'}
    env = {key: value for key, value in parent.items() if key.upper() in allowed}
    for key, name in {'HOME': 'home', 'USERPROFILE': 'home', 'XDG_CONFIG_HOME': 'config',
                      'XDG_CACHE_HOME': 'cache', 'HF_HOME': 'cache', 'TORCH_HOME': 'cache',
                      'APPDATA': 'appdata', 'LOCALAPPDATA': 'localappdata',
                      'TMPDIR': 'temp', 'TMP': 'temp', 'TEMP': 'temp'}.items():
        env[key] = str(directories[name])
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONNOUSERSITE='1')
    return env
