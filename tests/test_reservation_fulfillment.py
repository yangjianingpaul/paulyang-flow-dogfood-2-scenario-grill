"""Ticket #28 / #26/S8-S17: 兑现失败语义，以及归还与兑现彼此独立。"""

RESERVATION_FIELDS = {"id", "book_id", "holder", "status", "position"}
LOAN_FIELDS = {"id", "book_id", "borrower", "returned_at"}


def _book(http, base_url, title, initial_stock):
    _, _, body = http(
        "POST",
        f"{base_url}/books",
        {"title": title, "initial_stock": initial_stock},
    )
    return body


def _reserve(http, base_url, book_id, holder):
    return http("POST", f"{base_url}/reservations", {"book_id": book_id, "holder": holder})


def _fulfill(http, base_url, book_id):
    return http("POST", f"{base_url}/books/{book_id}/reservations/fulfill")


def _return(http, base_url, loan_id):
    return http("POST", f"{base_url}/loans/{loan_id}/return")


def _queued_book(http, base_url):
    """#26 前置条件 + S4/S6：库存被 seed Loan 耗尽，alice、bob 依次等待。"""
    book = _book(http, base_url, "三体-预约队列", 1)
    _, _, seed_loan = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "seed-borrower"}
    )
    _, _, alice = _reserve(http, base_url, book["id"], "alice")
    _, _, bob = _reserve(http, base_url, book["id"], "bob")
    return book, seed_loan, alice, bob


def test_s8_fulfilling_an_empty_queue_with_stock_reports_no_waiters(base_url, http):
    """#26/S8, DEC5/TC4: 有库存但空队列时兑现失败，明确提示没有等待者。"""
    book = _book(http, base_url, "有库存空队列", 1)

    status, headers, body = _fulfill(http, base_url, book["id"])

    assert status == 409
    assert headers["Content-Type"] == "application/json"
    assert isinstance(body["error"], str) and body["error"]
    assert "waiting" in body["error"].lower()
    assert set(body) == {"error"}


def test_s9_a_failed_fulfillment_leaves_the_stock_untouched(base_url, http):
    """#26/S9, TC4: 失败的兑现不改变 Book，available_stock 仍为 1。"""
    book = _book(http, base_url, "有库存空队列", 1)
    _fulfill(http, base_url, book["id"])

    status, _, after = http("GET", f"{base_url}/books/{book['id']}")

    assert status == 200
    assert after["available_stock"] == 1


def test_s10_an_empty_queue_without_stock_still_reports_no_waiters_first(base_url, http):
    """#26/S10, DEC5: 空队列优先失败为没有等待者，而不是库存耗尽。"""
    with_stock = _book(http, base_url, "有库存空队列", 1)
    without_stock = _book(http, base_url, "零库存空队列", 0)

    _, _, with_stock_error = _fulfill(http, base_url, with_stock["id"])
    status, _, body = _fulfill(http, base_url, without_stock["id"])

    assert status == 409
    assert "waiting" in body["error"].lower()
    assert "stock" not in body["error"].lower()
    assert body["error"] == with_stock_error["error"]


def test_s11_a_no_waiters_failure_leaves_zero_stock_at_zero(base_url, http):
    """#26/S11, TC4: 失败的兑现不改变 Book，available_stock 仍为 0。"""
    book = _book(http, base_url, "零库存空队列", 0)
    _fulfill(http, base_url, book["id"])

    status, _, after = http("GET", f"{base_url}/books/{book['id']}")

    assert status == 200
    assert after["available_stock"] == 0


def test_s12_fulfilling_a_queue_without_stock_reports_no_available_stock(base_url, http):
    """#26/S12, DEC6/TC4: 有等待者但无库存时失败，明确提示没有可用库存。"""
    book, _, _, _ = _queued_book(http, base_url)

    status, headers, body = _fulfill(http, base_url, book["id"])

    assert status == 409
    assert headers["Content-Type"] == "application/json"
    assert isinstance(body["error"], str) and body["error"]
    assert "stock" in body["error"].lower()
    assert set(body) == {"error"}


def test_s13_a_no_stock_failure_leaves_the_queue_identical(base_url, http):
    """#26/S13, DEC6/TC5: 失败的兑现不改变 Reservation 的 ID、顺序与位置。"""
    book, _, alice, bob = _queued_book(http, base_url)
    _, _, before = http("GET", f"{base_url}/books/{book['id']}/reservations")

    _fulfill(http, base_url, book["id"])

    status, _, after = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert status == 200
    assert after == before
    assert [(item["id"], item["position"]) for item in after] == [
        (alice["id"], 1),
        (bob["id"], 2),
    ]


def test_s14_a_no_stock_failure_creates_no_loan(base_url, http):
    """#26/S14, DEC6/TC4: 失败的兑现不创建 Loan，只剩 seed Loan。"""
    book, seed_loan, _, _ = _queued_book(http, base_url)

    _fulfill(http, base_url, book["id"])

    _, _, loans = http("GET", f"{base_url}/loans")
    assert [loan["id"] for loan in loans] == [seed_loan["id"]]
    assert [loan["borrower"] for loan in loans] == ["seed-borrower"]


def test_s15_returning_the_seed_loan_does_not_fulfill_anything(base_url, http):
    """#26/S15, DEC4/TC4: 归还只转换 Loan，响应没有新 Loan 或已兑现预约。"""
    book, seed_loan, _, _ = _queued_book(http, base_url)

    status, _, returned = _return(http, base_url, seed_loan["id"])

    assert status == 200
    assert returned["id"] == seed_loan["id"]
    assert returned["book_id"] == book["id"]
    assert returned["borrower"] == "seed-borrower"
    assert returned["returned_at"]
    assert set(returned) == LOAN_FIELDS


def test_s15_returning_creates_no_second_loan(base_url, http):
    """#26/S15 的不变量，DEC4: 归还没有自动借出给任何等待者。"""
    book, seed_loan, _, _ = _queued_book(http, base_url)

    _return(http, base_url, seed_loan["id"])

    _, _, loans = http("GET", f"{base_url}/loans")
    assert [loan["id"] for loan in loans] == [seed_loan["id"]]
    assert not [loan for loan in loans if loan["borrower"] in {"alice", "bob"}]


def test_s16_returning_restores_exactly_one_available_copy(base_url, http):
    """#26/S16, DEC4: 归还后 available_stock=1，没有被自动消耗。"""
    book, seed_loan, _, _ = _queued_book(http, base_url)

    _return(http, base_url, seed_loan["id"])

    _, _, after = http("GET", f"{base_url}/books/{book['id']}")
    assert after["available_stock"] == 1


def test_s17_returning_leaves_the_queue_untouched(base_url, http):
    """#26/S17, DEC4/TC4: 归还不转换 Reservation，等待者与位置不变。"""
    book, seed_loan, alice, bob = _queued_book(http, base_url)
    _, _, before = http("GET", f"{base_url}/books/{book['id']}/reservations")

    _return(http, base_url, seed_loan["id"])

    _, _, after = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert after == before
    assert [(item["id"], item["holder"], item["position"]) for item in after] == [
        (alice["id"], "alice", 1),
        (bob["id"], "bob", 2),
    ]


def test_a_failed_fulfillment_is_repeatable_without_side_effects(base_url, http):
    """TC4: 失败的兑现不改变任何状态，重复调用得到同样的结果。"""
    book, seed_loan, _, _ = _queued_book(http, base_url)

    first = _fulfill(http, base_url, book["id"])
    second = _fulfill(http, base_url, book["id"])

    assert first == second
    _, _, current = http("GET", f"{base_url}/books/{book['id']}")
    _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    _, _, loans = http("GET", f"{base_url}/loans")
    assert current["available_stock"] == 0
    assert len(queue) == 2
    assert [loan["id"] for loan in loans] == [seed_loan["id"]]


def test_fulfilling_an_unknown_book_is_404(base_url, http):
    """TC1: 未知 SKU 以可读错误拒绝，而非崩溃。"""
    status, _, body = _fulfill(http, base_url, "bk_404")

    assert status == 404
    assert isinstance(body["error"], str) and body["error"]


def test_each_sku_is_fulfilled_from_its_own_queue(base_url, http):
    """DEC1/TC3: 每个 SKU 拥有独立等待队列，兑现只看本 SKU。"""
    queued, _, _, _ = _queued_book(http, base_url)
    empty = _book(http, base_url, "三体-预约队列", 0)

    _, _, queued_error = _fulfill(http, base_url, queued["id"])
    _, _, empty_error = _fulfill(http, base_url, empty["id"])

    assert "stock" in queued_error["error"].lower()
    assert "waiting" in empty_error["error"].lower()


def test_fulfillment_consumes_one_copy_and_the_head_of_the_queue(base_url, http):
    """TC1/TC3/TC4: 一次兑现扣一个库存、移除队首并创建对应 Loan。"""
    book, seed_loan, alice, bob = _queued_book(http, base_url)
    _return(http, base_url, seed_loan["id"])

    status, _, body = _fulfill(http, base_url, book["id"])

    assert status == 201
    assert set(body) == {"reservation", "loan"}
    assert body["reservation"]["id"] == alice["id"]
    assert body["reservation"]["holder"] == "alice"
    assert set(body["reservation"]) == RESERVATION_FIELDS
    assert body["loan"]["book_id"] == book["id"]
    assert body["loan"]["borrower"] == "alice"
    assert body["loan"]["returned_at"] is None
    assert set(body["loan"]) == LOAN_FIELDS

    _, _, current = http("GET", f"{base_url}/books/{book['id']}")
    _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    _, _, loans = http("GET", f"{base_url}/loans")
    assert current["available_stock"] == 0
    assert [(item["id"], item["position"]) for item in queue] == [(bob["id"], 1)]
    assert [loan["id"] for loan in loans] == [seed_loan["id"], body["loan"]["id"]]


def test_fulfillment_failures_are_shared_across_instances(two_process_urls, http):
    """#26/S8-S17 跨实例，TC6: 两个实例读写同一份共享状态。"""
    (first_url, second_url), server_errors = two_process_urls
    book, seed_loan, alice, bob = _queued_book(http, first_url)

    _, _, no_stock = _fulfill(http, second_url, book["id"])
    _return(http, first_url, seed_loan["id"])
    _, _, current = http("GET", f"{second_url}/books/{book['id']}")
    _, _, queue = http("GET", f"{second_url}/books/{book['id']}/reservations")

    assert "stock" in no_stock["error"].lower()
    assert current["available_stock"] == 1
    assert [(item["id"], item["position"]) for item in queue] == [
        (alice["id"], 1),
        (bob["id"], 2),
    ]

    server_errors.seek(0)
    assert server_errors.read() == ""
