"""Ticket #63 / #61/S15-S26: 普通借阅一次到位地清掉过期预约并发书。

硬断言的唯一权威是 #61；本文件只落地它的回归测试 (TC13)。
用短有效期代替场景里的 30 秒，行为口径与 #61 一致：过期判定是
`expires_at <= 服务端当前时刻` (TC7, TC11)。
"""

import concurrent.futures
import json
import threading
import time
import urllib.error
import urllib.request

# 场景里的 30 秒在测试里缩成 2 秒。到期时刻按整秒截断，因此实际有效期落在
# 1–2 秒之间；等待 2.6 秒必然跨过它。
SHORT_TTL_SECONDS = 2
WAIT_PAST_TTL_SECONDS = 2.6

# 足够长，测试全程不会到期。
LONG_TTL_SECONDS = 3600

LOAN_FIELDS = {"id", "book_id", "borrower", "returned_at"}


def _book(http, base_url, title, initial_stock, ttl_seconds=None):
    body = {"title": title, "initial_stock": initial_stock}
    if ttl_seconds is not None:
        body["reservation_ttl_seconds"] = ttl_seconds
    return http("POST", f"{base_url}/books", body)


def _reserve(http, base_url, book_id, holder):
    return http(
        "POST", f"{base_url}/reservations", {"book_id": book_id, "holder": holder}
    )


def _restock(http, base_url, book_id, quantity):
    return http("POST", f"{base_url}/books/{book_id}/restock", {"quantity": quantity})


def _borrow(http, base_url, book_id, borrower):
    return http(
        "POST", f"{base_url}/loans", {"book_id": book_id, "borrower": borrower}
    )


def _expired_head_and_one_copy(http, base_url, holder="dave"):
    """#61/S15-S16 的等价 setup：唯一等待者已过期，架上恰好一本。"""
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _, _, waiter = _reserve(http, base_url, book["id"], holder)
    time.sleep(WAIT_PAST_TTL_SECONDS)
    _restock(http, base_url, book["id"], 1)
    return book, waiter


def test_s15_a_waiter_joins_an_empty_queue_at_position_one(base_url, http):
    """#61/S15: dave 登记成功，position 为 1。"""
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)

    status, _, dave = _reserve(http, base_url, book["id"], "dave")

    assert status == 201
    assert dave["position"] == 1
    assert dave["holder"] == "dave"


def test_s16_restocking_hands_the_copy_to_nobody(base_url, http):
    """#61/S16: 补货把库存加回 1，但不兑现任何等待者。"""
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _reserve(http, base_url, book["id"], "dave")

    status, _, restocked = _restock(http, base_url, book["id"], 1)

    assert status == 200
    assert restocked["available_stock"] == 1
    _, _, loans = http("GET", f"{base_url}/loans")
    assert loans == []


def test_s17_a_plain_borrow_clears_the_expired_head_and_succeeds_at_once(
    base_url, http
):
    """#61/S17: erin 当场借到，不需要借第二次 (TC9)。"""
    book, _ = _expired_head_and_one_copy(http, base_url)

    status, _, loan = _borrow(http, base_url, book["id"], "erin")

    assert status == 201
    assert loan["borrower"] == "erin"
    assert loan["book_id"] == book["id"]
    assert set(loan) == LOAN_FIELDS


def test_s18_the_expired_waiter_is_gone_from_the_queue_after_that_borrow(
    base_url, http
):
    """#61/S18: 队列为空数组 —— dave 在那一次借阅里被清掉了 (TC10)。"""
    book, _ = _expired_head_and_one_copy(http, base_url)

    _borrow(http, base_url, book["id"], "erin")

    status, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert status == 200
    assert queue == []


def test_s19_that_borrow_consumes_the_only_available_copy(base_url, http):
    """#61/S19: 借阅之后 available_stock 为 0。"""
    book, _ = _expired_head_and_one_copy(http, base_url)

    _borrow(http, base_url, book["id"], "erin")

    status, _, current = http("GET", f"{base_url}/books/{book['id']}")
    assert status == 200
    assert current["available_stock"] == 0


def test_s20_the_expired_waiter_never_produces_a_loan(base_url, http):
    """#61/S20: 只有借到手的人有 Loan，已过期的 dave 没有。"""
    book, _ = _expired_head_and_one_copy(http, base_url)

    _borrow(http, base_url, book["id"], "erin")

    status, _, loans = http("GET", f"{base_url}/loans")
    assert status == 200
    assert [loan["borrower"] for loan in loans] == ["erin"]


def test_s21_the_next_waiter_joins_the_emptied_queue_at_position_one(base_url, http):
    """#61/S21: 上一轮清空队列之后，frank 登记成功且 position 为 1。"""
    book, _ = _expired_head_and_one_copy(http, base_url)
    _borrow(http, base_url, book["id"], "erin")

    status, _, frank = _reserve(http, base_url, book["id"], "frank")

    assert status == 201
    assert frank["position"] == 1


def test_s22_restocking_after_a_borrow_puts_one_copy_back(base_url, http):
    """#61/S22: 补货之后 available_stock 为 1。"""
    book, _ = _expired_head_and_one_copy(http, base_url)
    _borrow(http, base_url, book["id"], "erin")
    _reserve(http, base_url, book["id"], "frank")

    status, _, restocked = _restock(http, base_url, book["id"], 1)

    assert status == 200
    assert restocked["available_stock"] == 1


def _concurrent_borrow(base_url, book_id, borrower, start):
    request = urllib.request.Request(
        f"{base_url}/loans",
        data=json.dumps({"book_id": book_id, "borrower": borrower}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start.wait()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return "success", response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return "failure", exc.code, json.loads(exc.read())
    except Exception as exc:  # surfaced by the assertions below
        return "transport_error", None, repr(exc)


def _race_two_borrowers(base_url, book_id, borrowers=("grace", "heidi")):
    start = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_concurrent_borrow, base_url, book_id, borrower, start)
            for borrower in borrowers
        ]
        start.set()
        return [future.result() for future in futures]


def test_s23_two_simultaneous_borrows_hand_out_the_single_copy_exactly_once(
    base_url, http
):
    """#61/S23: 恰好一个成功、一个被拒，架上只有一本 (TC6)。"""
    book, _ = _expired_head_and_one_copy(http, base_url, holder="frank")

    results = _race_two_borrowers(base_url, book["id"])

    kinds = sorted(kind for kind, _, _ in results)
    assert kinds == ["failure", "success"], results
    (winner,) = [body for kind, _, body in results if kind == "success"]
    (loser_status,) = [status for kind, status, _ in results if kind == "failure"]
    assert winner["borrower"] in {"grace", "heidi"}
    assert loser_status == 409
    _, _, loans = http("GET", f"{base_url}/loans")
    assert [loan["borrower"] for loan in loans] == [winner["borrower"]]


def test_s24_the_expired_waiter_is_purged_exactly_once_under_the_race(base_url, http):
    """#61/S24: 并发之后队列为空数组 —— 过期的 frank 恰好被清掉一次。"""
    book, frank = _expired_head_and_one_copy(http, base_url, holder="frank")

    _race_two_borrowers(base_url, book["id"])

    status, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert status == 200
    assert queue == []
    _, _, loans = http("GET", f"{base_url}/loans")
    assert frank["holder"] not in [loan["borrower"] for loan in loans]


def test_s25_the_race_consumes_exactly_one_copy(base_url, http):
    """#61/S25: 并发之后 available_stock 为 0。"""
    book, _ = _expired_head_and_one_copy(http, base_url, holder="frank")

    _race_two_borrowers(base_url, book["id"])

    status, _, current = http("GET", f"{base_url}/books/{book['id']}")
    assert status == 200
    assert current["available_stock"] == 0


def test_s26_each_race_round_adds_exactly_one_loan(base_url, http):
    """#61/S26: 重复单元 S21-S26 每跑一轮恰好只新增一笔 Loan（K=3）。"""
    book, _ = _expired_head_and_one_copy(http, base_url)
    _borrow(http, base_url, book["id"], "erin")

    borrowers = ["erin"]
    for _round in range(3):
        _reserve(http, base_url, book["id"], "frank")
        _restock(http, base_url, book["id"], 1)
        time.sleep(WAIT_PAST_TTL_SECONDS)

        results = _race_two_borrowers(base_url, book["id"])

        assert sorted(kind for kind, _, _ in results) == ["failure", "success"], results
        (winner,) = [body for kind, _, body in results if kind == "success"]
        assert winner["borrower"] in {"grace", "heidi"}
        borrowers.append(winner["borrower"])

        _, _, loans = http("GET", f"{base_url}/loans")
        assert len(loans) == len(borrowers)
        assert [loan["borrower"] for loan in loans] == borrowers
        _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
        assert queue == []
        _, _, current = http("GET", f"{base_url}/books/{book['id']}")
        assert current["available_stock"] == 0


def test_borrow_stays_rejected_when_an_unexpired_waiter_survives_the_cleanup(
    base_url, http
):
    """TC9: 清理之后队列仍非空时，既有的「队列非空即拒绝」原样成立。

    清理停在第一个未过期者 ivan 上，队列因此没有变空；这一次借阅被拒并回滚，
    连同清理一起回滚，dave 原样回到队首 (TC6, TC7)。
    """
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _, _, dave = _reserve(http, base_url, book["id"], "dave")
    time.sleep(WAIT_PAST_TTL_SECONDS)
    _restock(http, base_url, book["id"], 1)
    _, _, ivan = _reserve(http, base_url, book["id"], "ivan")

    status, _, body = _borrow(http, base_url, book["id"], "erin")

    assert status == 409
    assert body["code"] == "reservation_queue_active"
    assert body["next_action"] == "create_reservation"
    _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert [(item["id"], item["position"]) for item in queue] == [
        (dave["id"], 1),
        (ivan["id"], 2),
    ]
    _, _, loans = http("GET", f"{base_url}/loans")
    assert loans == []
    _, _, current = http("GET", f"{base_url}/books/{book['id']}")
    assert current["available_stock"] == 1


def test_borrow_is_rejected_for_stock_when_the_cleanup_empties_the_queue(
    base_url, http
):
    """TC9: 清理之后队列为空但没有可用库存时，按既有的库存耗尽被拒。"""
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _reserve(http, base_url, book["id"], "dave")
    time.sleep(WAIT_PAST_TTL_SECONDS)

    status, _, body = _borrow(http, base_url, book["id"], "erin")

    assert status == 409
    assert "stock exhausted" in body["error"].lower()
    assert set(body) == {"error"}
    _, _, loans = http("GET", f"{base_url}/loans")
    assert loans == []


def test_a_rejected_borrow_rolls_the_cleanup_back_with_it(base_url, http):
    """TC6: 事务回滚时清理一并回滚 —— 不存在「预约已清掉但书没发出去」的状态。"""
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _, _, dave = _reserve(http, base_url, book["id"], "dave")
    time.sleep(WAIT_PAST_TTL_SECONDS)

    # 清理会让队列变空，但库存耗尽使这一次借阅被拒并回滚。
    status, _, _ = _borrow(http, base_url, book["id"], "erin")

    assert status == 409
    _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert [(item["id"], item["holder"], item["position"]) for item in queue] == [
        (dave["id"], "dave", 1)
    ]


def test_a_long_lived_queue_still_blocks_a_plain_borrow(base_url, http):
    """TC9: 没有任何过期者时，本轮不改变既有的队列优先规则。"""
    _, _, book = _book(http, base_url, "Deep Work", 0, LONG_TTL_SECONDS)
    _, _, bob = _reserve(http, base_url, book["id"], "bob")
    _restock(http, base_url, book["id"], 1)

    status, _, body = _borrow(http, base_url, book["id"], "erin")

    assert status == 409
    assert body["code"] == "reservation_queue_active"
    _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert [item["id"] for item in queue] == [bob["id"]]
