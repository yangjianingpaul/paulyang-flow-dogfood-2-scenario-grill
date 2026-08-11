import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import app


def _unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_serving(process, port):
    deadline = time.monotonic() + 10
    url = f"http://127.0.0.1:{port}/nope"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"app.py exited during startup with code {process.returncode}"
            )
        try:
            urllib.request.urlopen(url, timeout=0.2)
        except urllib.error.HTTPError:
            return
        except urllib.error.URLError:
            time.sleep(0.02)
    raise AssertionError(f"app.py did not start on port {port}")


@pytest.fixture
def two_process_urls():
    """Start two app.py processes that share this worktree's SQLite file."""
    app.reset_state()
    root = Path(app.__file__).resolve().parent
    ports = (_unused_port(), _unused_port())
    with tempfile.TemporaryFile(mode="w+") as server_errors:
        processes = [
            subprocess.Popen(
                [sys.executable, "app.py", "--port", str(port)],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=server_errors,
                text=True,
            )
            for port in ports
        ]
        try:
            for process, port in zip(processes, ports):
                _wait_until_serving(process, port)
            urls = tuple(f"http://127.0.0.1:{port}" for port in ports)
            yield urls, server_errors
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


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
