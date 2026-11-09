"""Read actual local Work runtime metadata; never trust model tool arguments.

This is a local host adapter, not cryptographic remote attestation. Deployment
must protect the session directory from the Work's writes. A missing host thread
ID, unsupported log format, stale log, or different model/effort fails closed.
The real Dot launch still needs end-to-end validation before production use.
"""
import json
from pathlib import Path
import re
import time


class ModelGate:
    def __init__(self, sessions_root, *, max_idle_seconds=180,expected_effort='medium'):
        self.root = Path(sessions_root).resolve(strict=True)
        self.max_idle_seconds = max_idle_seconds
        self.expected_effort = expected_effort

    def check(self, thread_id):
        if not isinstance(thread_id, str) or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', thread_id):
            raise PermissionError('Host thread identity unavailable')
        matches = list(self.root.glob('**/rollout-*-' + thread_id + '.jsonl'))
        if len(matches) != 1: raise PermissionError('Unique host runtime record required')
        path = matches[0].resolve(strict=True)
        if not path.is_relative_to(self.root): raise PermissionError('Host runtime record escaped trusted directory')
        stat = path.stat()
        if abs(time.time()-stat.st_mtime) > self.max_idle_seconds:
            raise PermissionError('Host runtime record is stale')
        meta = context = None
        # Stream metadata without copying conversation or credentials to logs.
        # Partial writes are rejected for retry: they could contain a new turn
        # with another model. Never accidentally retain an older allowed model.
        with path.open('rb') as stream:
            while True:
                raw = stream.readline(16*1024*1024+1)
                if not raw: break
                if len(raw)>16*1024*1024: raise PermissionError('Unsupported host record size')
                if not raw.endswith(b'\n'): raise PermissionError('Host runtime write incomplete; retry preflight')
                try: record = json.loads(raw)
                except (ValueError, UnicodeError): raise PermissionError('Invalid host runtime record') from None
                if record.get('type')=='session_meta': meta = record.get('payload')
                if record.get('type')=='turn_context': context = record.get('payload')
        if not isinstance(meta, dict) or meta.get('id') != thread_id or not isinstance(context, dict):
            raise PermissionError('Host session/turn metadata missing or mismatched')
        if context.get('model') != 'gpt-6.1-sol' or (self.expected_effort is not None and context.get('effort')!=self.expected_effort):
            raise PermissionError('Actual host model or reasoning effort does not match the configured supervisor')
        if context.get('effort') not in ('low','medium','high','xhigh','max','ultra'):
            raise PermissionError('Actual host reasoning effort is unknown')
        turn = context.get('turn_id')
        if not isinstance(turn, str) or not turn: raise PermissionError('Host turn identity missing')
        return {'thread': thread_id, 'turn': turn, 'model': context['model'], 'effort': context['effort'],
                'source': 'local-host-turn-context', 'checkedAt': time.time()}
