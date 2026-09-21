import io
import json
import os
import sys
import threading
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eudamed import fakeserver                      # noqa: E402
from eudamed.client import Client                   # noqa: E402


class FakeResp(io.BytesIO):
    """Minimal stand-in for an http.client.HTTPResponse."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def json_opener(payload, status=200):
    def opener(req, timeout=None):
        resp = FakeResp(json.dumps(payload).encode())
        resp.status = status
        return resp
    return opener


def error_opener(code, body=b"", headers=None, times=None):
    """Raise HTTPError; after `times` raises, succeed with an empty list."""
    state = {"n": 0}

    def opener(req, timeout=None):
        state["n"] += 1
        if times is not None and state["n"] > times:
            return FakeResp(b"[]")
        raise urllib.error.HTTPError(req.full_url, code, "err", headers or {}, io.BytesIO(body))
    opener.state = state
    return opener


@pytest.fixture
def client_factory():
    def make(opener=None, **kw):
        kw.setdefault("base", "https://example.invalid/eudamed")
        kw.setdefault("key", "testkey")
        kw.setdefault("delay", 0)
        kw.setdefault("retries", 2)
        kw.setdefault("backoff_base", 0)
        return Client(opener=opener, **kw)
    return make


@pytest.fixture(scope="session")
def live_server():
    """The bundled fake API on a real socket, for end-to-end tests."""
    server = fakeserver.serve(port=0, require_key=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}/eudamed"
    server.shutdown()
    server.server_close()


@pytest.fixture
def devices_csv(tmp_path):
    path = tmp_path / "devices.csv"
    path.write_text(
        "name,description,ca,country,keys,broad\n"
        "MindDoc,psych software,Bavaria DE,DE,MindDoc,Mind Doc\n"
        "Kalmeda,tinnitus,NRW DE,DE,Kalmeda,\n"
        "NotRegistered,invented,XX,DE,ZZZNotARealDevice,\n",
        encoding="utf-8")
    return str(path)
