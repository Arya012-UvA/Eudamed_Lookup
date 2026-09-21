"""HTTP client for the EUDAMED Public API v1.0."""

import contextlib
import csv
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

UA = f"eudamed-lookup/{__import__('eudamed').__version__} (+https://github.com/Arya012-UvA/Eudamed_Lookup)"

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    """A request failed. Carries the real cause instead of discarding it."""

    def __init__(self, message, url=None, status=None, body=None, cause=None):
        super().__init__(message)
        self.url = url
        self.status = status
        self.body = body
        self.cause = cause


class AuthError(ApiError):
    """401/403 - the subscription key is missing, wrong, or lacks access."""


class Client:
    def __init__(self, base=None, key=None, auth_mode="header", fmt="json",
                 delay=0.2, retries=4, timeout=60, verbose=False,
                 api_version=config.API_VERSION, opener=None, backoff_base=0.5):
        if fmt not in config.FORMATS:
            raise ValueError(f"format must be one of {config.FORMATS}, got {fmt!r}")
        if auth_mode not in ("header", "query"):
            raise ValueError(f"auth_mode must be 'header' or 'query', got {auth_mode!r}")
        self.base = (base or config.default_base()).rstrip("/")
        self.key = key if key is not None else config.default_key()
        self.auth_mode = auth_mode
        self.fmt = fmt
        self.delay = max(0.0, delay)
        self.retries = max(1, retries)          # always at least one attempt
        self.timeout = timeout
        self.verbose = verbose
        self.api_version = api_version
        self.backoff_base = max(0.0, backoff_base)
        self._opener = opener or urllib.request.urlopen
        self._last_request = 0.0
        self.request_count = 0

    # -- URL construction -------------------------------------------------
    def build_url(self, path, params=None):
        """Full request URL, including the required format and api-version."""
        allowed = config.OPERATIONS.get(path)
        query = {}
        for name, value in (params or {}).items():
            if value is None or value == "":
                continue
            if allowed is not None and name not in allowed:
                raise ValueError(
                    f"{name!r} is not a documented parameter of {path}; "
                    f"allowed: {', '.join(allowed)}"
                )
            query[name] = value
        query["format"] = self.fmt                     # required by the spec
        if self.api_version:
            query["api-version"] = self.api_version    # required by the gateway
        if self.key and self.auth_mode == "query":
            query[config.KEY_QUERY] = self.key
        return f"{self.base}{path}?" + urllib.parse.urlencode(query)

    def _headers(self):
        headers = {
            "Accept": "application/json" if self.fmt == "json" else "text/csv",
            "Content-Type": "application/json",   # required: true, per the spec
            "User-Agent": UA,
        }
        if self.key and self.auth_mode == "header":
            headers[config.KEY_HEADER] = self.key
        return headers

    # -- transport --------------------------------------------------------
    def _throttle(self):
        """Space requests by --delay, measured from the previous request.

        Unlike a plain sleep-before-each-call this costs nothing on the first
        request and nothing when the caller was already slow.
        """
        if not self.delay:
            return
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def request(self, path, params=None):
        """GET an operation and return (rows, raw_body_text).

        Raises AuthError on 401/403, ApiError on anything else that does not
        succeed within --retries attempts. The original exception is always
        attached as .cause and named in the message.
        """
        url = self.build_url(path, params)
        safe_url = self._redact(url)
        last = None
        for attempt in range(self.retries):
            if attempt:
                back = (self.delay or self.backoff_base) * (2 ** attempt)
                if back:
                    time.sleep(min(back, 30))
            self._throttle()
            self._last_request = time.monotonic()
            self.request_count += 1
            try:
                req = urllib.request.Request(url, headers=self._headers())
                with self._opener(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
                if self.verbose:
                    print(f"  GET {safe_url} -> {getattr(resp, 'status', 200)} "
                          f"({len(body)} bytes)", flush=True)
                return self._parse(body, safe_url), body
            except urllib.error.HTTPError as exc:
                detail = self._read_error(exc)
                last = exc
                if exc.code in (401, 403):
                    raise AuthError(
                        f"{exc.code} {exc.reason} from {safe_url}. The EUDAMED Public "
                        f"API requires a subscription key - pass --key or set "
                        f"${config.KEY_ENV}." + (f" Response: {detail}" if detail else ""),
                        url=safe_url, status=exc.code, body=detail, cause=exc) from exc
                if exc.code not in RETRY_STATUS:
                    raise ApiError(
                        f"{exc.code} {exc.reason} from {safe_url}"
                        + (f": {detail}" if detail else ""),
                        url=safe_url, status=exc.code, body=detail, cause=exc) from exc
                retry_after = (exc.headers or {}).get("Retry-After") if exc.headers else None
                if retry_after:
                    with contextlib.suppress(TypeError, ValueError):
                        time.sleep(min(float(retry_after), 60))
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    json.JSONDecodeError, csv.Error, UnicodeDecodeError) as exc:
                last = exc
        raise ApiError(
            f"{safe_url} failed after {self.retries} attempt(s): "
            f"{type(last).__name__}: {last}",
            url=safe_url, status=getattr(last, "code", None), cause=last)

    def _parse(self, body, safe_url):
        if self.fmt == "csv":
            rows = list(csv.DictReader(io.StringIO(body)))
            return [r for r in rows if any((v or "").strip() for v in r.values())]
        text = body.strip()
        if not text:
            return []
        from .fields import unwrap_rows
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ApiError(
                f"{safe_url} returned {len(body)} bytes that are not JSON "
                f"({exc}). First 200 chars: {text[:200]!r}",
                url=safe_url, body=body[:2000], cause=exc) from exc
        return unwrap_rows(payload)

    @staticmethod
    def _read_error(exc):
        """Body of an error response, best effort.

        Only ever used to enrich a message that is being raised anyway, so a
        failure to read it must not mask the original error.
        """
        try:
            return exc.read().decode("utf-8", "replace")[:500]
        except (OSError, AttributeError, ValueError, UnicodeError):
            return ""

    def _redact(self, url):
        if not self.key:
            return url
        quoted = urllib.parse.quote(self.key, safe="")
        return url.replace(quoted, "<key>").replace(self.key, "<key>")

    # -- typed operations -------------------------------------------------
    def udi(self, **params):
        return self.request("/udi", params)

    def actors(self, **params):
        return self.request("/actors", params)

    def reference(self, **params):
        return self.request("/reference", params)
