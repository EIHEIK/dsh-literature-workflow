"""Fixed DeepSeek endpoint. One ledger dispatch corresponds to one HTTP attempt.

No SDK retries, environment proxies, redirects, arbitrary URLs, or raw upstream
errors. This executor never receives SQLite or the worker's filesystem paths.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def retry_delay(value):
    try: return max(0,min(float(value),3600))
    except (ValueError,TypeError):
        try: return max(0,min((parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds(),3600))
        except (ValueError,TypeError,OverflowError): return 0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class DeepSeekHTTP:
    def __init__(self,credential,*,proxy=None,timeout=240):
        if not isinstance(credential,str) or len(credential)<16 or any(c.isspace() for c in credential):
            raise ValueError('Provider credential is unavailable')
        self._credential=credential;self.timeout=timeout
        proxies={}
        if proxy:
            parsed=urlsplit(proxy)
            if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or not parsed.port or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
                raise ValueError('Only a configured local HTTP CONNECT proxy is supported')
            proxies={'https':proxy}
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler(proxies),NoRedirect(),
                                               urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def __call__(self,payload):
        data=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        if len(data)>8*1024*1024: return {'kind':'unknown','reason':'payload_limit','retryable':False}
        request=urllib.request.Request('https://api.deepseek.com/chat/completions',data=data,
            headers={'Authorization':'Bearer '+self._credential,'Content-Type':'application/json','Accept':'application/json'},method='POST')
        try:
            with self.opener.open(request,timeout=self.timeout) as result:
                if result.status!=200: return {'kind':'unknown','reason':'unexpected_status','retryable':False}
                raw=result.read(16*1024*1024+1)
            if len(raw)>16*1024*1024: return {'kind':'unknown','reason':'response_limit','retryable':False}
            response=json.loads(raw)
            if not isinstance(response,dict):raise ValueError('Non-object response')
            return {'kind':'response','body':response}
        except urllib.error.HTTPError as error:
            code=error.code;delay=retry_delay(error.headers.get('Retry-After'));error.close()
            reason=('authentication' if code in (401,403) else 'rate_limit' if code==429 else
                    'provider_unavailable' if code>=500 else 'request_rejected')
            return {'kind':'unknown','reason':reason,'retryable':code==429,'retryAfter':delay}
        except (urllib.error.URLError,TimeoutError,ConnectionError,OSError):
            return {'kind':'unknown','reason':'transport','retryable':True}
        except (ValueError,UnicodeError):
            return {'kind':'unknown','reason':'invalid_response','retryable':False}
