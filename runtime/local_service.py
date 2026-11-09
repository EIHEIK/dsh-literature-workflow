"""Authenticated loopback transport; never binds a public network interface.

Provisioned credentials belong to local adapters, not the language model. A
separate credential is reserved for verified host confirmation responses. This
transport does not itself attest a Dot model or prove a human clicked a button;
the installed adapter must supply those guarantees before production enablement.
"""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


class LocalService:
    def __init__(self, controller, api, credentials, *, dot_gate=None,codex_gate=None, port=0):
        if not credentials or any(role not in ('dsh', 'dot', 'codex', 'confirmation','maintenance') for role in credentials):
            raise ValueError('Explicit adapter credentials required')
        if any(not isinstance(token, str) or len(token) < 40 for token in credentials.values()):
            raise ValueError('Strong adapter credentials required')
        if len(set(credentials.values())) != len(credentials): raise ValueError('Adapter credentials must differ')
        self.controller = controller; self.api = api
        self.credentials = dict(credentials); self.dot_gate = dot_gate
        self.codex_gate = codex_gate
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def setup(self):
                super().setup(); self.connection.settimeout(10)

            def do_POST(self):
                code = 400
                try:
                    if self.headers.get('Origin') is not None: raise PermissionError('Browser requests are not supported')
                    if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port):
                        raise PermissionError('Invalid local host')
                    if self.path not in ('/rpc', '/confirmation','/maintenance/stop'): raise ValueError('Unsupported local endpoint')
                    authorization = self.headers.get('Authorization', '')
                    role = next((role for role, token in service.credentials.items()
                                 if hmac.compare_digest(authorization, 'Bearer ' + token)), None)
                    if role is None: raise PermissionError('Local adapter authentication required')
                    if self.headers.get('Transfer-Encoding') is not None or self.headers.get('Content-Type') != 'application/json':
                        raise ValueError('Only bounded JSON requests are supported')
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 65536: raise ValueError('Invalid request size')
                    body = self.rfile.read(size)
                    if len(body) != size: raise ValueError('Incomplete request body')
                    request = json.loads(body)
                    if self.path=='/maintenance/stop':
                        if role!='maintenance' or request!={'action':'stop'}:raise PermissionError('Maintenance credential required')
                        service.controller.stop();result={'stopping':True}
                    elif self.path == '/confirmation':
                        if role != 'confirmation': raise PermissionError('Trusted user confirmation adapter required')
                        if not isinstance(request, dict) or set(request) != {'batch', 'summaryHash', 'receipt'}:
                            raise ValueError('Invalid confirmation response')
                        result = service.controller.submit(lambda store: store.confirm(
                            request['batch'], request['summaryHash'], request['receipt']))
                    else:
                        actor=None
                        if role in ('confirmation','maintenance'): raise PermissionError('This credential cannot control jobs')
                        if role == 'dot':
                            if service.dot_gate is None: raise PermissionError('Dot host/model validation is not installed')
                            evidence=service.dot_gate(self.headers.get('X-Literature-Host-Thread'))
                            actor='dot:'+evidence['thread']+':'+evidence['turn']
                        if role == 'codex':
                            if service.codex_gate is None:raise PermissionError('Codex supervisor verification is not installed')
                            evidence=service.codex_gate(self.headers.get('X-Literature-Host-Thread'))
                            actor='codex:'+evidence['thread']+':'+evidence['turn']
                        if not isinstance(request, dict) or set(request) != {'method', 'arguments'}:
                            raise ValueError('Invalid control request')
                        result = service.controller.submit(lambda store: service.api.call(
                            store, request['method'], request['arguments'],actor=actor))
                    response = {'result': result}; code = 200
                except PermissionError:
                    response = {'error': {'kind': 'permission', 'message': 'Adapter permission or host verification failed'}}; code = 403
                except (ValueError, KeyError, TypeError):
                    response = {'error': {'kind': 'invalid_request', 'message': 'Unsupported arguments, state or evidence'}}
                except Exception:
                    # No exception text, paths, response bodies, or credentials in
                    # transport errors. Detailed structured events stay local.
                    response = {'error': {'kind': 'unavailable', 'message': 'Controller unavailable; inspect status before retrying'}}; code = 503
                encoded = json.dumps(response, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(encoded)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try: self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError): pass

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True

    @property
    def url(self): return 'http://127.0.0.1:' + str(self.server.server_port)

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
