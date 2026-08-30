"""Ticket #62 / #61/S11-S14: 兑现队首时清掉过期预约，把书发给第一个未过期者。

硬断言的唯一权威是 #61；本文件只落地它的回归测试 (TC13)。
用短有效期代替场景里的 30 秒，行为口径与 #61 一致：过期判定是
`expires_at <= 服务端当前时刻` (TC7)。
"""

import time

# 场景里的 30 秒在测试里缩成 2 秒。到期时刻按整秒截断，因此实际有效期落在
# 1–2 秒之间；等待 2.6 秒必然跨过它，而随后登记的人还剩 1 秒以上的余量。
SHORT_TTL_SECONDS = 2
WAIT_PAST_TTL_SECONDS = 2.6

# 足够长，测试全程不会到期。
LONG_TTL_SECONDS = 3600

LOAN_FIELDS = {"id", "book_id", "borrower", "returned_at"}
RESERVATION_FIELDS = {"id", "book_id", "holder", "status", "position"}


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


def _fulfill(http, base_url, book_id):
    return http("POST", f"{base_url}/books/{book_id}/reservations/fulfill")


def _expired_head_then_fresh_waiter(http, base_url):
    """#61/S1-S10 的等价 setup：队首 alice 已过期，carol 未过期，架上一本。"""
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _, _, alice = _reserve(http, base_url, book["id"], "alice")
    time.sleep(WAIT_PAST_TTL_SECONDS)
    _restock(http, base_url, book["id"], 1)
    _, _, carol = _reserve(http, base_url, book["id"], "carol")
    return book, alice, carol


def test_s11_fulfillment_skips_the_expired_head_and_serves_the_first_fresh_waiter(
    base_url, http
):
    """#61/S11: 兑现交出去的是 carol，已过期的 alice 拿不到书。"""
    book, alice, carol = _expired_head_then_fresh_waiter(http, base_url)

    status, _, body = _fulfill(http, base_url, book["id"])

    assert status == 201
    assert set(body) == {"reservation", "loan"}
    assert body["reservation"]["id"] == carol["id"]
    assert body["reservation"]["holder"] == "carol"
    assert set(body["reservation"]) == RESERVATION_FIELDS
    assert body["loan"]["borrower"] == "carol"
    assert body["loan"]["book_id"] == book["id"]
    assert set(body["loan"]) == LOAN_FIELDS
    assert alice["id"] != body["reservation"]["id"]


def test_s11_the_expired_head_never_produces_a_loan(base_url, http):
    """#61/S11: 没有为已过期的 alice 产生任何 Loan。"""
    book, _, _ = _expired_head_then_fresh_waiter(http, base_url)

    _fulfill(http, base_url, book["id"])

    _, _, loans = http("GET", f"{base_url}/loans")
    assert [loan["borrower"] for loan in loans] == ["carol"]


def test_s12_the_queue_is_empty_after_fulfillment(base_url, http):
    """#61/S12: 过期的 alice 与被兑现的 carol 都不在队列里。"""
    book, _, _ = _expired_head_then_fresh_waiter(http, base_url)

    _fulfill(http, base_url, book["id"])

    status, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert status == 200
    assert queue == []


def test_s13_fulfillment_consumes_the_only_available_copy(base_url, http):
    """#61/S13: 兑现之后 available_stock 为 0。"""
    book, _, _ = _expired_head_then_fresh_waiter(http, base_url)

    _fulfill(http, base_url, book["id"])

    status, _, current = http("GET", f"{base_url}/books/{book['id']}")
    assert status == 200
    assert current["available_stock"] == 0


def test_s14_exactly_one_loan_exists_and_it_belongs_to_carol(base_url, http):
    """#61/S14: 全库恰好一笔 Loan，borrower 为 carol。"""
    book, _, _ = _expired_head_then_fresh_waiter(http, base_url)

    _fulfill(http, base_url, book["id"])

    _, _, loans = http("GET", f"{base_url}/loans")
    assert len(loans) == 1
    assert loans[0]["borrower"] == "carol"
    assert loans[0]["book_id"] == book["id"]


def test_fulfillment_reports_no_waiters_when_only_expired_reservations_remain(
    base_url, http
):
    """TC8: 清理之后队列为空时，兑现按既有「没有等待者」被拒。"""
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _reserve(http, base_url, book["id"], "alice")
    time.sleep(WAIT_PAST_TTL_SECONDS)
    _restock(http, base_url, book["id"], 1)

    status, _, body = _fulfill(http, base_url, book["id"])

    assert status == 409
    assert "waiting" in body["error"].lower()
    _, _, loans = http("GET", f"{base_url}/loans")
    assert loans == []
    _, _, current = http("GET", f"{base_url}/books/{book['id']}")
    assert current["available_stock"] == 1


def test_cleanup_stops_at_the_first_unexpired_waiter(base_url, http):
    """TC7: 只删除队首起连续的已过期段，其余等待者一行不动。"""
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _reserve(http, base_url, book["id"], "alice")
    _reserve(http, base_url, book["id"], "bob")
    time.sleep(WAIT_PAST_TTL_SECONDS)
    _restock(http, base_url, book["id"], 1)
    _, _, carol = _reserve(http, base_url, book["id"], "carol")
    _, _, dave = _reserve(http, base_url, book["id"], "dave")

    _, _, body = _fulfill(http, base_url, book["id"])

    assert body["reservation"]["holder"] == "carol"
    _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert [(item["id"], item["position"]) for item in queue] == [(dave["id"], 1)]
    assert carol["id"] not in [item["id"] for item in queue]


def test_expiry_is_scoped_to_the_sku_that_declared_it(base_url, http):
    """TC3: 有效期属于 SKU；长有效期的书上队首不会被清掉。"""
    _, _, short_lived = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _, _, long_lived = _book(http, base_url, "Deep Work", 0, LONG_TTL_SECONDS)
    _reserve(http, base_url, short_lived["id"], "alice")
    _, _, bob = _reserve(http, base_url, long_lived["id"], "bob")
    time.sleep(WAIT_PAST_TTL_SECONDS)
    _restock(http, base_url, long_lived["id"], 1)

    _, _, body = _fulfill(http, base_url, long_lived["id"])

    assert body["reservation"]["id"] == bob["id"]
    assert body["loan"]["borrower"] == "bob"


def test_each_reservation_on_a_sku_expires_on_its_own_clock(base_url, http):
    """TC4: 每条预约各自独立计时，与它在队列中的位置无关。"""
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _reserve(http, base_url, book["id"], "alice")
    time.sleep(WAIT_PAST_TTL_SECONDS)
    _, _, carol = _reserve(http, base_url, book["id"], "carol")
    _restock(http, base_url, book["id"], 1)

    _, _, queue_before = http("GET", f"{base_url}/books/{book['id']}/reservations")
    _, _, body = _fulfill(http, base_url, book["id"])

    assert [item["holder"] for item in queue_before] == ["alice", "carol"]
    assert body["reservation"]["id"] == carol["id"]


def test_registering_a_reservation_does_not_clean_up_expired_ones(base_url, http):
    """TC5: 登记不触发清理，在已过期者之后登记的人排在他后面。"""
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _reserve(http, base_url, book["id"], "alice")
    time.sleep(WAIT_PAST_TTL_SECONDS)

    status, _, carol = _reserve(http, base_url, book["id"], "carol")

    assert status == 201
    assert carol["position"] == 2


def test_queries_never_hide_expired_reservations(base_url, http):
    """TC10: 队列查询不按是否过期筛选，也不新增字段。"""
    _, _, book = _book(http, base_url, "Atomic Habits", 0, SHORT_TTL_SECONDS)
    _, _, alice = _reserve(http, base_url, book["id"], "alice")
    time.sleep(WAIT_PAST_TTL_SECONDS)

    status, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")

    assert status == 200
    assert [(item["id"], item["holder"], item["position"]) for item in queue] == [
        (alice["id"], "alice", 1)
    ]
    assert set(queue[0]) == RESERVATION_FIELDS


def test_creating_a_book_without_a_ttl_still_succeeds_and_does_not_expire(
    base_url, http
):
    """TC12: 不带有效期字段的既有建书写法一字不改，行为不变。"""
    status, _, book = _book(http, base_url, "Deep Work", 0)
    assert status == 201
    assert set(book) == {"id", "title", "available_stock"}

    _, _, bob = _reserve(http, base_url, book["id"], "bob")
    time.sleep(WAIT_PAST_TTL_SECONDS)
    _restock(http, base_url, book["id"], 1)

    _, _, body = _fulfill(http, base_url, book["id"])

    assert body["reservation"]["id"] == bob["id"]
    assert body["loan"]["borrower"] == "bob"


def test_a_non_positive_integer_ttl_is_rejected(base_url, http):
    """TC1: 出现但不是正整数时 400，错误体沿用既有形状。"""
    for invalid in (0, -1, 1.5, "30", True, None):
        # 显式给出 null 同样算「出现」，因此这里绕开 _book 的省略逻辑。
        status, headers, body = http(
            "POST",
            f"{base_url}/books",
            {
                "title": "Atomic Habits",
                "initial_stock": 0,
                "reservation_ttl_seconds": invalid,
            },
        )
        assert status == 400, invalid
        assert headers["Content-Type"] == "application/json"
        assert isinstance(body["error"], str) and body["error"]
        assert set(body) == {"error"}


def test_a_valid_ttl_creates_the_book_without_changing_the_response_shape(
    base_url, http
):
    """TC1: 建书响应不新增字段。"""
    status, _, book = _book(base_url=base_url, http=http, title="Atomic Habits",
                            initial_stock=0, ttl_seconds=30)

    assert status == 201
    assert set(book) == {"id", "title", "available_stock"}
    assert book["available_stock"] == 0
