import json
import threading
import urllib.error
import urllib.request

import pytest

import app


@pytest.fixture
def base_url():
    """Start app.py's real HTTP server in-process on an ephemeral port (TC23)."""
    app.reset_state()
    server = app.make_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def http():
    def request(method, url, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, dict(resp.headers), json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, dict(exc.headers), json.loads(raw) if raw else None

    return request
