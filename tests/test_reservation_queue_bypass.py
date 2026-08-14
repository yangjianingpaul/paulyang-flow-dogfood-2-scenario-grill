"""Ticket #29 / #26/S18-S21: 有等待队列时普通借阅一律不得绕过。"""


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


def _fulfill(http, base_url, book_id):
    return http("POST", f"{base_url}/books/{book_id}/reservations/fulfill")


def _return(http, base_url, loan_id):
    return http("POST", f"{base_url}/loans/{loan_id}/return")


def _queued_book_with_one_copy(http, base_url):
    """#26/S1-S17 的状态：alice、bob 在等待，归还已让出一个可用库存。"""
    book = _book(http, base_url, "三体-预约队列", 1)
    _, _, seed_loan = _borrow(http, base_url, book["id"], "seed-borrower")
    _, _, alice = _reserve(http, base_url, book["id"], "alice")
    _, _, bob = _reserve(http, base_url, book["id"], "bob")
    _return(http, base_url, seed_loan["id"])
    return book, seed_loan, alice, bob


def test_s18_an_outsider_cannot_borrow_past_the_queue(base_url, http):
    """#26/S18, DEC11/TC3: mallory 的普通借阅失败，并提示应通过兑现队首处理。"""
    book, _, _, _ = _queued_book_with_one_copy(http, base_url)

    status, headers, body = _borrow(http, base_url, book["id"], "mallory")

    assert status == 409
    assert headers["Content-Type"] == "application/json"
    assert isinstance(body["error"], str) and body["error"]
    assert "queue" in body["error"].lower()
    assert "fulfil" in body["error"].lower()
    assert set(body) == {"error"}


def test_s19_the_head_of_the_queue_cannot_borrow_directly_either(base_url, http):
    """#26/S19, DEC11: 队首 alice 本人同样被拒，且提示与 S18 逐字相同。"""
    book, _, _, _ = _queued_book_with_one_copy(http, base_url)
    _, _, outsider_error = _borrow(http, base_url, book["id"], "mallory")

    status, _, body = _borrow(http, base_url, book["id"], "alice")

    assert status == 409
    assert "queue" in body["error"].lower()
    assert "fulfil" in body["error"].lower()
    assert body["error"] == outsider_error["error"]


def test_s20_blocked_borrow_attempts_leave_the_stock_untouched(base_url, http):
    """#26/S20, TC3/TC4: 两次被拒的普通借阅都不消耗库存，仍为 1。"""
    book, _, _, _ = _queued_book_with_one_copy(http, base_url)

    _borrow(http, base_url, book["id"], "mallory")
    _borrow(http, base_url, book["id"], "alice")

    status, _, after = http("GET", f"{base_url}/books/{book['id']}")
    assert status == 200
    assert after["available_stock"] == 1


def test_s21_blocked_borrow_attempts_create_no_loan(base_url, http):
    """#26/S21, TC4: 只剩已归还的 seed Loan，没有 mallory 或新建 alice Loan。"""
    book, seed_loan, _, _ = _queued_book_with_one_copy(http, base_url)

    _borrow(http, base_url, book["id"], "mallory")
    _borrow(http, base_url, book["id"], "alice")

    _, _, loans = http("GET", f"{base_url}/loans")
    assert [loan["id"] for loan in loans] == [seed_loan["id"]]
    assert loans[0]["returned_at"]
    assert not [loan for loan in loans if loan["borrower"] in {"mallory", "alice"}]


def test_a_blocked_borrow_leaves_the_queue_identical(base_url, http):
    """TC4: 失败的普通借阅不转换任何 Reservation。"""
    book, _, alice, bob = _queued_book_with_one_copy(http, base_url)
    _, _, before = http("GET", f"{base_url}/books/{book['id']}/reservations")

    _borrow(http, base_url, book["id"], "mallory")

    _, _, after = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert after == before
    assert [(item["id"], item["position"]) for item in after] == [
        (alice["id"], 1),
        (bob["id"], 2),
    ]


def test_the_queue_blocks_borrowing_before_stock_is_considered(base_url, http):
    """DEC11/TC3: 队列非空即拒绝，与该 SKU 当前是否有库存无关。"""
    book = _book(http, base_url, "三体-预约队列", 1)
    _, _, seed_loan = _borrow(http, base_url, book["id"], "seed-borrower")
    _reserve(http, base_url, book["id"], "alice")

    without_stock = _borrow(http, base_url, book["id"], "mallory")
    _return(http, base_url, seed_loan["id"])
    with_stock = _borrow(http, base_url, book["id"], "mallory")

    assert without_stock[0] == with_stock[0] == 409
    assert without_stock[2]["error"] == with_stock[2]["error"]
    assert "queue" in without_stock[2]["error"].lower()


def test_an_empty_queue_still_allows_direct_borrowing(base_url, http):
    """TC3/DEC2: 只有等待队列才阻断普通借阅，空队列的 SKU 不受影响。"""
    queued, _, _, _ = _queued_book_with_one_copy(http, base_url)
    free = _book(http, base_url, "有库存空队列", 1)

    status, _, loan = _borrow(http, base_url, free["id"], "mallory")

    assert status == 201
    assert loan["book_id"] == free["id"]
    _, _, blocked = _borrow(http, base_url, queued["id"], "mallory")
    assert "queue" in blocked["error"].lower()


def test_borrowing_reopens_once_the_queue_is_emptied(base_url, http):
    """DEC11/TC3: 阻断由等待队列造成；队列清空后普通借阅恢复正常。"""
    book, _, _, _ = _queued_book_with_one_copy(http, base_url)
    _, _, alice_fulfillment = _fulfill(http, base_url, book["id"])  # alice 出队
    _return(http, base_url, alice_fulfillment["loan"]["id"])

    blocked_status, _, blocked = _borrow(http, base_url, book["id"], "mallory")
    _, _, bob_fulfillment = _fulfill(http, base_url, book["id"])  # bob 出队，队列清空
    _return(http, base_url, bob_fulfillment["loan"]["id"])

    status, _, loan = _borrow(http, base_url, book["id"], "mallory")

    assert blocked_status == 409
    assert "queue" in blocked["error"].lower()
    assert status == 201
    assert loan["borrower"] == "mallory"


def test_borrowing_an_unknown_book_is_still_404(base_url, http):
    """TC1: 未知 SKU 仍以 404 拒绝，队列判断不改变这一点。"""
    status, _, body = _borrow(http, base_url, "bk_404", "mallory")

    assert status == 404
    assert isinstance(body["error"], str) and body["error"]


def test_the_queue_guard_is_shared_across_instances(two_process_urls, http):
    """#26/S18-S21 跨实例，TC6: 另一个实例登记的队列同样阻断普通借阅。"""
    (first_url, second_url), server_errors = two_process_urls
    book, seed_loan, alice, bob = _queued_book_with_one_copy(http, first_url)

    _, _, outsider = _borrow(http, second_url, book["id"], "mallory")
    _, _, head = _borrow(http, second_url, book["id"], "alice")
    _, _, current = http("GET", f"{second_url}/books/{book['id']}")
    _, _, loans = http("GET", f"{second_url}/loans")

    assert "queue" in outsider["error"].lower()
    assert head["error"] == outsider["error"]
    assert current["available_stock"] == 1
    assert [loan["id"] for loan in loans] == [seed_loan["id"]]

    server_errors.seek(0)
    assert server_errors.read() == ""
