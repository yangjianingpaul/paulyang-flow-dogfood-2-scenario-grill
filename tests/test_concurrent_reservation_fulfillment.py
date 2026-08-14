"""Ticket #30 / #26/S22-S25: 两实例并发兑现队首，共享状态收敛到唯一结果。"""

import concurrent.futures
import json
import threading
import urllib.error
import urllib.request

RESERVATION_FIELDS = {"id", "book_id", "holder", "status", "position"}
LOAN_FIELDS = {"id", "book_id", "borrower", "returned_at"}


def _book(http, base_url, title, initial_stock):
    _, _, body = http(
        "POST",
        f"{base_url}/books",
        {"title": title, "initial_stock": initial_stock},
    )
    return body


def _borrow(http, base_url, book_id, borrower):
    return http(
        "POST",
        f"{base_url}/loans",
        {"book_id": book_id, "borrower": borrower},
    )


def _reserve(http, base_url, book_id, holder):
    return http(
        "POST",
        f"{base_url}/reservations",
        {"book_id": book_id, "holder": holder},
    )


def _return(http, base_url, loan_id):
    return http("POST", f"{base_url}/loans/{loan_id}/return")


def _state_through_s21(http, first_url, second_url):
    """#26/S1-S21 之后的共享状态：alice、bob 在等待，恰好一个可用库存。

    前序步骤只用于建立状态，本模块不重新验收它们。
    """
    book = _book(http, first_url, "三体-预约队列", 1)
    _, _, seed_loan = _borrow(http, first_url, book["id"], "seed-borrower")
    _, _, alice = _reserve(http, first_url, book["id"], "alice")  # S4
    _reserve(http, second_url, book["id"], "alice")  # S5：复用同一条预约
    _, _, bob = _reserve(http, second_url, book["id"], "bob")  # S6
    _return(http, first_url, seed_loan["id"])  # S15：让出唯一一个库存
    _borrow(http, second_url, book["id"], "mallory")  # S18：被队列拒绝
    _borrow(http, first_url, book["id"], "alice")  # S19：队首本人同样被拒
    return book, seed_loan, alice, bob


def _fulfill_from(url, book_id, barrier):
    """在 barrier 上对齐后立刻发出兑现请求，模拟 #26/S22 的两实例同时到达。"""
    request = urllib.request.Request(
        f"{url}/books/{book_id}/reservations/fulfill",
        method="POST",
    )
    try:
        barrier.wait()
        with urllib.request.urlopen(request, timeout=30) as response:
            return {
                "instance": url,
                "status": response.status,
                "body": json.loads(response.read()),
                "transport_error": None,
            }
    except urllib.error.HTTPError as exc:
        return {
            "instance": url,
            "status": exc.code,
            "body": json.loads(exc.read()),
            "transport_error": None,
        }
    except Exception as exc:  # 由下面的断言原样呈现
        return {
            "instance": url,
            "status": None,
            "body": None,
            "transport_error": repr(exc),
        }


def _race_fulfillments(urls, book_id):
    """两个实例几乎同时兑现同一个队首，返回两条观察结果。"""
    barrier = threading.Barrier(len(urls))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(urls)) as pool:
        return list(
            pool.map(lambda url: _fulfill_from(url, book_id, barrier), urls)
        )


def test_s22_every_concurrent_fulfillment_gets_a_definite_http_result(
    two_process_urls, http
):
    """#26/S22, TC6: 两请求都有明确 HTTP 结果，没有 transport error。"""
    urls, server_errors = two_process_urls
    book, _, _, _ = _state_through_s21(http, *urls)

    results = _race_fulfillments(urls, book["id"])

    assert [result["transport_error"] for result in results] == [None, None], results
    assert all(isinstance(result["status"], int) for result in results), results
    server_errors.seek(0)
    assert server_errors.read() == ""


def test_s22_exactly_one_concurrent_fulfillment_succeeds(two_process_urls, http):
    """#26/S22, DEC12/TC3: 两实例竞争唯一库存时严格一个成功。"""
    urls, _ = two_process_urls
    book, _, _, _ = _state_through_s21(http, *urls)

    results = _race_fulfillments(urls, book["id"])

    assert sorted(result["status"] for result in results) == [201, 409], results


def test_s22_the_winner_fulfills_alice_and_creates_her_loan(two_process_urls, http):
    """#26/S22, DEC3/DEC7/DEC8: 成功的一方兑现队首 alice 并创建同 SKU 的 alice Loan。"""
    urls, _ = two_process_urls
    book, _, alice, _ = _state_through_s21(http, *urls)

    results = _race_fulfillments(urls, book["id"])
    winner = next(result for result in results if result["status"] == 201)

    assert set(winner["body"]) == {"reservation", "loan"}
    assert winner["body"]["reservation"]["id"] == alice["id"]
    assert winner["body"]["reservation"]["holder"] == "alice"
    assert set(winner["body"]["reservation"]) == RESERVATION_FIELDS
    assert winner["body"]["loan"]["book_id"] == book["id"]
    assert winner["body"]["loan"]["borrower"] == "alice"
    assert winner["body"]["loan"]["returned_at"] is None
    assert set(winner["body"]["loan"]) == LOAN_FIELDS


def test_s22_the_loser_fails_on_exhausted_stock(two_process_urls, http):
    """#26/S22, DEC12/TC4: 另一个请求明确因库存耗尽失败。"""
    urls, _ = two_process_urls
    book, _, _, _ = _state_through_s21(http, *urls)

    results = _race_fulfillments(urls, book["id"])
    loser = next(result for result in results if result["status"] == 409)

    assert isinstance(loser["body"]["error"], str) and loser["body"]["error"]
    assert "stock" in loser["body"]["error"].lower()
    assert set(loser["body"]) == {"error"}


def test_s22_the_outcome_does_not_depend_on_which_instance_wins(
    two_process_urls, http
):
    """#26/S22, DEC12: 哪个实例成功不构成判据，收敛结果与赢家身份无关。"""
    urls, server_errors = two_process_urls
    second_url = urls[1]

    winners = []
    converged = set()
    for _ in range(20):
        book, _, alice, bob = _state_through_s21(http, *urls)
        results = _race_fulfillments(urls, book["id"])
        assert all(result["transport_error"] is None for result in results), results
        winner = next(
            (result for result in results if result["status"] == 201), None
        )
        assert winner is not None, results
        _, _, queue = http("GET", f"{second_url}/books/{book['id']}/reservations")
        _, _, current = http("GET", f"{second_url}/books/{book['id']}")

        winners.append(winner["instance"])
        # 每轮的实体 ID 不同，因此按角色而非 ID 描述收敛后的状态。
        converged.add(
            (
                tuple(sorted(result["status"] for result in results)),
                winner["body"]["reservation"]["id"] == alice["id"],
                winner["body"]["loan"]["borrower"],
                tuple(
                    (item["id"] == bob["id"], item["holder"], item["position"])
                    for item in queue
                ),
                current["available_stock"],
            )
        )

    # 赢家身份可以逐轮不同，收敛后的状态必须逐轮完全一致。
    assert converged == {
        ((201, 409), True, "alice", ((True, "bob", 1),), 0)
    }, converged
    assert set(winners) <= set(urls)

    server_errors.seek(0)
    assert server_errors.read() == ""


def test_s23_only_bob_remains_waiting_at_position_one(two_process_urls, http):
    """#26/S23, DEC10/TC5: 并发兑现后只剩 bob，其 position=1，alice 不再等待。"""
    urls, _ = two_process_urls
    second_url = urls[1]
    book, _, alice, bob = _state_through_s21(http, *urls)

    _race_fulfillments(urls, book["id"])

    status, _, queue = http("GET", f"{second_url}/books/{book['id']}/reservations")
    assert status == 200
    assert [(item["id"], item["holder"], item["position"]) for item in queue] == [
        (bob["id"], "bob", 1)
    ]
    assert alice["id"] not in {item["id"] for item in queue}


def test_s24_the_shared_stock_converges_to_zero(two_process_urls, http):
    """#26/S24, DEC12/TC3: 并发兑现只消耗一个库存，available_stock=0 且不为负。"""
    urls, _ = two_process_urls
    first_url = urls[0]
    book, _, _, _ = _state_through_s21(http, *urls)

    _race_fulfillments(urls, book["id"])

    status, _, after = http("GET", f"{first_url}/books/{book['id']}")
    assert status == 200
    assert after["available_stock"] == 0


def test_s25_exactly_one_new_loan_belongs_to_alice(two_process_urls, http):
    """#26/S25, DEC7/DEC12: 已归还 seed Loan 加恰好一笔 alice 的新 Loan。"""
    urls, _ = two_process_urls
    second_url = urls[1]
    book, seed_loan, _, _ = _state_through_s21(http, *urls)

    results = _race_fulfillments(urls, book["id"])
    winner = next(result for result in results if result["status"] == 201)

    status, _, loans = http("GET", f"{second_url}/loans")
    assert status == 200
    assert [loan["id"] for loan in loans] == [seed_loan["id"], winner["body"]["loan"]["id"]]
    assert loans[0]["returned_at"]
    new_loan = loans[1]
    assert new_loan["book_id"] == book["id"]
    assert new_loan["borrower"] == "alice"
    assert new_loan["returned_at"] is None
    assert not [loan for loan in loans if loan["borrower"] in {"bob", "mallory"}]


def test_repeated_races_never_double_fulfill_a_single_copy(two_process_urls, http):
    """DEC12/TC3: 重复竞争同一个可用库存时，始终严格一次成功且库存不为负。"""
    urls, server_errors = two_process_urls
    first_url, second_url = urls
    book, _, alice, bob = _state_through_s21(http, *urls)
    outcomes = []
    for _ in range(8):
        results = _race_fulfillments(urls, book["id"])
        assert all(result["transport_error"] is None for result in results), results
        outcomes.append(sorted(result["status"] for result in results))
        winner = next(
            (result for result in results if result["status"] == 201), None
        )
        if winner is None:
            break
        # 归还刚兑现出的 Loan，让下一轮重新竞争同一个可用库存。
        _return(http, first_url, winner["body"]["loan"]["id"])

    # 队列里只有 alice 与 bob，因此只有前两轮能成功兑现。
    assert outcomes[:2] == [[201, 409], [201, 409]]
    assert all(statuses == [409, 409] for statuses in outcomes[2:])

    _, _, queue = http("GET", f"{second_url}/books/{book['id']}/reservations")
    _, _, current = http("GET", f"{second_url}/books/{book['id']}")
    _, _, loans = http("GET", f"{second_url}/loans")
    assert queue == []
    assert current["available_stock"] == 1
    assert sorted(loan["borrower"] for loan in loans) == [
        "alice",
        "bob",
        "seed-borrower",
    ]
    assert {alice["id"], bob["id"]}.isdisjoint({loan["id"] for loan in loans})

    server_errors.seek(0)
    assert server_errors.read() == ""


def test_concurrent_fulfillment_of_an_empty_queue_never_succeeds(
    two_process_urls, http
):
    """DEC5/TC4: 空队列上的并发兑现两侧都失败为没有等待者，不创建任何 Loan。"""
    urls, _ = two_process_urls
    first_url, _ = urls
    book = _book(http, first_url, "有库存空队列", 1)

    results = _race_fulfillments(urls, book["id"])

    assert [result["status"] for result in results] == [409, 409]
    assert all(
        "waiting" in result["body"]["error"].lower() for result in results
    ), results
    _, _, current = http("GET", f"{first_url}/books/{book['id']}")
    _, _, loans = http("GET", f"{first_url}/loans")
    assert current["available_stock"] == 1
    assert loans == []
