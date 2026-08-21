"""Ticket #43 / #42/S6-S8: 队列里还有别人时，处置队首并兑现新队首。"""

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


def _defer(http, base_url, book_id):
    return http("POST", f"{base_url}/books/{book_id}/reservations/defer")


def _fulfill(http, base_url, book_id):
    return http("POST", f"{base_url}/books/{book_id}/reservations/fulfill")


def _locked_book(http, base_url):
    """#42/S1-S4：库存 0 的 SKU，乙、丙依次等待，随后进货 1 本形成锁死局面。"""
    book = _book(http, base_url, "深入理解计算机系统", 0)
    _, _, yi = _reserve(http, base_url, book["id"], "乙")
    _, _, bing = _reserve(http, base_url, book["id"], "丙")
    http("POST", f"{base_url}/books/{book['id']}/restock", {"quantity": 1})
    return book, yi, bing


def test_s5_the_locked_copy_cannot_be_borrowed_by_anyone(base_url, http):
    """#42/S5：处置之前这一本谁都拿不走，锁死局面确实成立。"""
    book, _, _ = _locked_book(http, base_url)

    status, _, body = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "丁"}
    )

    assert 400 <= status < 500
    assert "id" not in body


def test_s6_deferring_the_head_sends_them_to_the_tail(base_url, http):
    """#42/S6, DEC1/DEC2/TC1: 处置成功，乙仍在等待中且落到队尾 position 2。"""
    book, yi, _ = _locked_book(http, base_url)

    status, headers, body = _defer(http, base_url, book["id"])

    assert 200 <= status < 300
    assert headers["Content-Type"] == "application/json"
    assert body["holder"] == "乙"
    assert body["status"] == "waiting"
    assert body["position"] == 2
    assert body["id"] == yi["id"]
    assert body["book_id"] == book["id"]


def test_s6_deferring_hands_out_no_loan(base_url, http):
    """#42/S6, DEC4/TC4: 这一步不发书 —— 响应里没有 Loan，库存与 Loan 集合不变。"""
    book, _, _ = _locked_book(http, base_url)

    _, _, body = _defer(http, base_url, book["id"])

    assert set(body) == RESERVATION_FIELDS
    _, _, loans = http("GET", f"{base_url}/loans")
    _, _, current = http("GET", f"{base_url}/books/{book['id']}")
    assert loans == []
    assert current["available_stock"] == 1


def test_s6_deferring_keeps_the_waiting_set_element_for_element(base_url, http):
    """#42/S6, DEC1/TC3/TC4: 处置不新建也不删除等待预约，id 与 holder 原样保留。"""
    book, yi, bing = _locked_book(http, base_url)

    _defer(http, base_url, book["id"])

    _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert {item["id"] for item in queue} == {yi["id"], bing["id"]}
    assert sorted(item["holder"] for item in queue) == sorted(["乙", "丙"])


def test_s7_the_queue_shows_the_new_order(base_url, http):
    """#42/S7, DEC2/TC5/TC8: 队列按处置后的顺序返回 丙(1)、乙(2)，两人都在等待中。"""
    book, yi, bing = _locked_book(http, base_url)
    _defer(http, base_url, book["id"])

    status, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")

    assert status == 200
    assert [(item["holder"], item["position"]) for item in queue] == [
        ("丙", 1),
        ("乙", 2),
    ]
    assert [item["id"] for item in queue] == [bing["id"], yi["id"]]
    assert {item["status"] for item in queue} == {"waiting"}
    assert all(set(item) == RESERVATION_FIELDS for item in queue)


def test_s8_fulfilling_hands_the_copy_to_the_new_head(base_url, http):
    """#42/S8, DEC4/TC7: 处置之后兑现，这一本借给新队首丙。"""
    book, _, bing = _locked_book(http, base_url)
    _defer(http, base_url, book["id"])

    status, _, body = _fulfill(http, base_url, book["id"])

    assert 200 <= status < 300
    assert body["reservation"]["id"] == bing["id"]
    assert body["reservation"]["holder"] == "丙"
    assert body["reservation"]["status"] != "waiting"
    assert body["loan"]["borrower"] == "丙"
    assert body["loan"]["book_id"] == book["id"]
    assert set(body["loan"]) == LOAN_FIELDS


def test_s8_fulfilling_the_new_head_leaves_the_deferred_reader_waiting(base_url, http):
    """#42/S8, DEC1/TC7: 兑现只移出丙，乙仍在等待队列里并回到队首。"""
    book, yi, _ = _locked_book(http, base_url)
    _defer(http, base_url, book["id"])

    _fulfill(http, base_url, book["id"])

    _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    _, _, current = http("GET", f"{base_url}/books/{book['id']}")
    _, _, loans = http("GET", f"{base_url}/loans")
    assert [(item["id"], item["holder"], item["position"]) for item in queue] == [
        (yi["id"], "乙", 1)
    ]
    assert current["available_stock"] == 0
    assert [loan["borrower"] for loan in loans] == ["丙"]


def test_deferring_is_repeatable_and_rotates_the_queue(base_url, http):
    """DEC2/TC5: 每次处置都把当时的队首送到队尾，队列整体前移一位。"""
    book, yi, bing = _locked_book(http, base_url)

    _defer(http, base_url, book["id"])
    _, _, second = _defer(http, base_url, book["id"])

    assert second["id"] == bing["id"]
    assert second["position"] == 2
    _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert [(item["id"], item["position"]) for item in queue] == [
        (yi["id"], 1),
        (bing["id"], 2),
    ]


def test_each_sku_is_deferred_from_its_own_queue(base_url, http):
    """TC3: 每个 SKU 拥有独立等待队列，处置只动本 SKU。"""
    book, yi, bing = _locked_book(http, base_url)
    other = _book(http, base_url, "另一个 SKU", 0)
    _, _, other_yi = _reserve(http, base_url, other["id"], "乙")

    _defer(http, base_url, book["id"])

    _, _, other_queue = http("GET", f"{base_url}/books/{other['id']}/reservations")
    assert [(item["id"], item["position"]) for item in other_queue] == [
        (other_yi["id"], 1)
    ]
    _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert [item["id"] for item in queue] == [bing["id"], yi["id"]]


def test_deferring_an_unknown_book_is_404(base_url, http):
    """TC1: 未知 SKU 以可读错误拒绝，而非崩溃。"""
    status, _, body = _defer(http, base_url, "bk_404")

    assert status == 404
    assert isinstance(body["error"], str) and body["error"]


def test_deferring_an_empty_queue_reports_no_waiters(base_url, http):
    """TC1: 没有等待者时处置失败，语义与兑现的空队列失败一致。"""
    book = _book(http, base_url, "零库存空队列", 0)

    status, _, body = _defer(http, base_url, book["id"])

    assert status == 409
    assert "waiting" in body["error"].lower()
    assert set(body) == {"error"}


def test_the_deferred_order_is_shared_across_instances(two_process_urls, http):
    """#42/S6-S8 跨请求可见，TC9: 处置改写的队列序与库存、Loan 同库持久化。"""
    (first_url, second_url), server_errors = two_process_urls
    book, yi, bing = _locked_book(http, first_url)

    _, _, deferred = _defer(http, first_url, book["id"])
    _, _, queue = http("GET", f"{second_url}/books/{book['id']}/reservations")
    _, _, fulfillment = _fulfill(http, second_url, book["id"])

    assert deferred["holder"] == "乙"
    assert [(item["id"], item["position"]) for item in queue] == [
        (bing["id"], 1),
        (yi["id"], 2),
    ]
    assert fulfillment["loan"]["borrower"] == "丙"

    server_errors.seek(0)
    assert server_errors.read() == ""
