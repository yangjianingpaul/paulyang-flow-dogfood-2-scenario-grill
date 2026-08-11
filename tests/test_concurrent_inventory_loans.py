"""Ticket #12 / #9/S2: shared inventory bounds concurrent borrowing."""

import concurrent.futures
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
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


def _borrow(base_url, book_id, client_no, start):
    request = urllib.request.Request(
        f"{base_url}/loans",
        data=json.dumps(
            {"book_id": book_id, "borrower": f"client-{client_no:04d}"}
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start.wait()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return "success", base_url, response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return "failure", base_url, exc.code, json.loads(exc.read())
    except Exception as exc:  # surfaced by the assertions below
        return "transport_error", base_url, None, repr(exc)


def test_borrow_consumes_stock_until_it_is_exhausted(base_url, http):
    """#9/S2, TC3-TC4: each success consumes one unit, then stock is exhausted."""
    _, _, book = http(
        "POST",
        f"{base_url}/books",
        {"title": "三体", "initial_stock": 2},
    )

    first = http(
        "POST",
        f"{base_url}/loans",
        {"book_id": book["id"], "borrower": "client-0001"},
    )
    second = http(
        "POST",
        f"{base_url}/loans",
        {"book_id": book["id"], "borrower": "client-0002"},
    )
    exhausted = http(
        "POST",
        f"{base_url}/loans",
        {"book_id": book["id"], "borrower": "client-0003"},
    )

    assert [first[0], second[0], exhausted[0]] == [201, 201, 409]
    assert {first[2]["borrower"], second[2]["borrower"]} == {
        "client-0001",
        "client-0002",
    }
    assert "stock exhausted" in exhausted[2]["error"].lower()


def test_two_processes_atomically_share_inventory_for_1000_clients(
    two_process_urls, http
):
    """#9/S2, TC3/TC4/TC6: exactly 500 of 1000 cross-process borrows win."""
    (first_url, second_url), server_errors = two_process_urls
    status, _, book = http(
        "POST",
        f"{first_url}/books",
        {"title": "三体", "initial_stock": 500},
    )
    assert status == 201

    # Interleave destinations so active workers exercise both processes together.
    attempts = [
        item
        for client in range(1, 501)
        for item in ((first_url, client), (second_url, client + 500))
    ]
    start = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as pool:
        futures = [
            pool.submit(_borrow, url, book["id"], client_no, start)
            for url, client_no in attempts
        ]
        start.set()
        results = [future.result() for future in futures]

    counts = Counter(kind for kind, _, _, _ in results)
    destinations = Counter(url for _, url, _, _ in results)
    successes = [body for kind, _, _, body in results if kind == "success"]
    failures = [body for kind, _, _, body in results if kind == "failure"]
    transport_errors = [
        (url, body)
        for kind, url, _, body in results
        if kind == "transport_error"
    ]
    server_errors.flush()
    server_errors.seek(0)
    server_error_output = server_errors.read()

    assert destinations == {first_url: 500, second_url: 500}
    assert counts == {"success": 500, "failure": 500}, (
        transport_errors,
        server_error_output,
    )
    assert all(status == 201 for kind, _, status, _ in results if kind == "success")
    assert all(status == 409 for kind, _, status, _ in results if kind == "failure")
    assert all(
        isinstance(body.get("id"), str)
        and body["id"]
        and body["book_id"] == book["id"]
        and isinstance(body.get("borrower"), str)
        for body in successes
    )
    assert all(
        isinstance(body, dict)
        and "stock exhausted" in body.get("error", "").lower()
        for body in failures
    )

    list_status, _, loans = http("GET", f"{second_url}/loans")
    assert list_status == 200
    assert len(loans) == 500
    assert len({loan["id"] for loan in loans}) == 500
    assert {loan["borrower"] for loan in loans} == {
        body["borrower"] for body in successes
    }
