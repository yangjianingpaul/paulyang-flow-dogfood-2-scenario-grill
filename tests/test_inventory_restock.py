"""Ticket #23 / #20/S5-S8: restock shared inventory and preserve its bound."""

import concurrent.futures
import json
import urllib.error
import urllib.request
from collections import Counter
from threading import Barrier


def _restock(http, base_url, book_id, quantity=2):
    return http(
        "POST",
        f"{base_url}/books/{book_id}/restock",
        {"quantity": quantity},
    )


def _borrow(base_url, book_id, borrower, barrier):
    request = urllib.request.Request(
        f"{base_url}/loans",
        data=json.dumps({"book_id": book_id, "borrower": borrower}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    barrier.wait()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return "success", response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return "failure", exc.code, json.loads(exc.read())
    except Exception as exc:  # surfaced by the assertions below
        return "transport_error", None, repr(exc)


def test_s5_restock_increments_the_current_inventory(two_process_urls, http):
    """#20/S5, TC1/TC3-TC5: adding two to current stock one returns stock three."""
    (first_url, _), _ = two_process_urls
    _, _, book = http(
        "POST",
        f"{first_url}/books",
        {"title": "三体", "initial_stock": 1},
    )

    status, headers, body = _restock(http, first_url, book["id"])

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body == {
        "id": book["id"],
        "title": "三体",
        "available_stock": 3,
    }


def test_s6_second_process_reads_restocked_inventory(two_process_urls, http):
    """#20/S6, TC2-TC4/TC6-TC7: the other instance reads shared stock three."""
    (first_url, second_url), _ = two_process_urls
    _, _, book = http(
        "POST",
        f"{first_url}/books",
        {"title": "三体", "initial_stock": 1},
    )
    restock_status, _, _ = _restock(http, first_url, book["id"])
    assert restock_status == 200

    status, headers, body = http("GET", f"{second_url}/books/{book['id']}")

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body == {
        "id": book["id"],
        "title": "三体",
        "available_stock": 3,
    }


def test_s7_s8_concurrent_borrows_stop_at_restocked_bound(
    two_process_urls, http
):
    """#20/S7-S8, TC2-TC7: three wins, two exhausted, then shared stock zero."""
    (first_url, second_url), server_errors = two_process_urls
    _, _, book = http(
        "POST",
        f"{first_url}/books",
        {"title": "三体", "initial_stock": 1},
    )
    restock_status, _, _ = _restock(http, first_url, book["id"])
    assert restock_status == 200

    attempts = [
        (first_url, "client-0001"),
        (second_url, "client-0002"),
        (first_url, "client-0003"),
        (second_url, "client-0004"),
        (first_url, "client-0005"),
    ]
    barrier = Barrier(len(attempts))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(attempts)) as pool:
        futures = [
            pool.submit(_borrow, url, book["id"], borrower, barrier)
            for url, borrower in attempts
        ]
        results = [future.result() for future in futures]

    server_errors.flush()
    server_errors.seek(0)
    server_error_output = server_errors.read()
    counts = Counter(kind for kind, _, _ in results)
    successes = [body for kind, _, body in results if kind == "success"]
    failures = [
        (status, body) for kind, status, body in results if kind == "failure"
    ]

    assert counts == {"success": 3, "failure": 2}, server_error_output
    assert all(status == 201 for kind, status, _ in results if kind == "success")
    assert len(successes) == 3
    assert all(
        status == 409
        and isinstance(body, dict)
        and "stock exhausted" in body.get("error", "").lower()
        for status, body in failures
    )
    assert not [result for result in results if result[0] == "transport_error"]

    status, _, final_book = http("GET", f"{second_url}/books/{book['id']}")
    assert status == 200
    assert final_book["available_stock"] == 0
