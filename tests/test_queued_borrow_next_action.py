"""Ticket #37 / #36/S4-S6: 被队列挡住的普通借阅带回可执行的下一步。"""


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


def _restock(http, base_url, book_id, quantity):
    return http(
        "POST",
        f"{base_url}/books/{book_id}/restock",
        {"quantity": quantity},
    )


def _stocked_book_with_a_queue(http, base_url):
    """#36 前置条件 + S1：库存 1、队列 alice(1)/bob(2)、seed Loan 未归还。"""
    book = _book(http, base_url, "三体-队列优先", 1)
    _, _, seed_loan = _borrow(http, base_url, book["id"], "seed-borrower")
    _, _, alice = _reserve(http, base_url, book["id"], "alice")
    _, _, bob = _reserve(http, base_url, book["id"], "bob")
    _restock(http, base_url, book["id"], 1)
    return book, seed_loan, alice, bob


def test_s4_an_outsider_is_told_to_create_a_reservation(base_url, http):
    """#36/S4, TC1/TC5: carol 未在队列中，被拒响应给出 create_reservation。"""
    book, _, _, _ = _stocked_book_with_a_queue(http, base_url)

    status, headers, body = _borrow(http, base_url, book["id"], "carol")

    assert status == 409
    assert headers["Content-Type"] == "application/json"
    assert isinstance(body["error"], str) and body["error"]
    assert "queue" in body["error"].lower()
    assert body["code"] == "reservation_queue_active"
    assert body["next_action"] == "create_reservation"
    assert set(body) == {"error", "code", "next_action"}


def test_s4_a_blocked_borrow_creates_no_loan(base_url, http):
    """#36/S4, TC3: 被拒的借阅没有产生任何新 Loan。"""
    book, seed_loan, _, _ = _stocked_book_with_a_queue(http, base_url)

    _borrow(http, base_url, book["id"], "carol")

    _, _, loans = http("GET", f"{base_url}/loans")
    assert [loan["id"] for loan in loans] == [seed_loan["id"]]
    assert not [loan for loan in loans if loan["borrower"] == "carol"]


def test_s5_a_blocked_borrow_leaves_the_stock_untouched(base_url, http):
    """#36/S5, TC3: 被拒的借阅没有消耗库存，仍为 1。"""
    book, _, _, _ = _stocked_book_with_a_queue(http, base_url)

    _borrow(http, base_url, book["id"], "carol")

    status, _, after = http("GET", f"{base_url}/books/{book['id']}")
    assert status == 200
    assert after["available_stock"] == 1


def test_s5_a_blocked_borrow_leaves_the_queue_identical(base_url, http):
    """#36/S5, TC3: 被拒的借阅不改动队列的任何一条。"""
    book, _, alice, bob = _stocked_book_with_a_queue(http, base_url)
    _, _, before = http("GET", f"{base_url}/books/{book['id']}/reservations")

    _borrow(http, base_url, book["id"], "carol")

    _, _, after = http("GET", f"{base_url}/books/{book['id']}/reservations")
    assert after == before
    assert [(item["id"], item["holder"], item["position"]) for item in after] == [
        (alice["id"], "alice", 1),
        (bob["id"], "bob", 2),
    ]


def test_s6_the_head_of_the_queue_is_told_to_wait(base_url, http):
    """#36/S6, TC5: 队首 alice 同样 409，但下一步是 wait_for_fulfillment。"""
    book, _, _, _ = _stocked_book_with_a_queue(http, base_url)
    _, _, outsider = _borrow(http, base_url, book["id"], "carol")

    status, _, body = _borrow(http, base_url, book["id"], "alice")

    assert status == 409
    assert isinstance(body["error"], str) and body["error"]
    assert body["error"] == outsider["error"]
    assert body["code"] == "reservation_queue_active"
    assert body["next_action"] == "wait_for_fulfillment"
    assert set(body) == {"error", "code", "next_action"}


def test_s6_the_head_of_the_queue_creates_no_loan(base_url, http):
    """#36/S6, TC5: 队首身份不构成例外，没有产生 alice 的 Loan。"""
    book, seed_loan, _, _ = _stocked_book_with_a_queue(http, base_url)

    _borrow(http, base_url, book["id"], "alice")

    _, _, loans = http("GET", f"{base_url}/loans")
    assert [loan["id"] for loan in loans] == [seed_loan["id"]]
    assert not [loan for loan in loans if loan["borrower"] == "alice"]


def test_a_non_head_waiter_is_also_told_to_wait(base_url, http):
    """TC5: 队首与非队首不作区分，bob 同样得到 wait_for_fulfillment。"""
    book, _, _, _ = _stocked_book_with_a_queue(http, base_url)

    _, _, body = _borrow(http, base_url, book["id"], "bob")

    assert body["next_action"] == "wait_for_fulfillment"


def test_next_action_compares_the_borrower_verbatim(base_url, http):
    """TC2/TC5: 是否已在队列中一律以逐字字符串相等判定。"""
    book, _, _, _ = _stocked_book_with_a_queue(http, base_url)

    _, _, upper = _borrow(http, base_url, book["id"], "Alice")
    _, _, padded = _borrow(http, base_url, book["id"], "alice ")

    assert upper["next_action"] == "create_reservation"
    assert padded["next_action"] == "create_reservation"


def test_next_action_is_scoped_to_the_requested_sku(base_url, http):
    """TC2/TC5: 队列成员身份按 book_id + borrower 判定，不跨 SKU。"""
    book, _, _, _ = _stocked_book_with_a_queue(http, base_url)
    other, _, _, _ = _stocked_book_with_a_queue(http, base_url)
    _reserve(http, base_url, other["id"], "carol")

    _, _, body = _borrow(http, base_url, book["id"], "carol")

    assert body["next_action"] == "create_reservation"


def test_the_other_rejection_branches_are_unchanged(base_url, http):
    """TC1: 未知 SKU 的 404 与库存耗尽的 409 本轮形状不变。"""
    unknown_status, _, unknown = _borrow(http, base_url, "bk_404", "carol")
    free = _book(http, base_url, "有库存空队列", 1)
    _borrow(http, base_url, free["id"], "dave")

    exhausted_status, _, exhausted = _borrow(http, base_url, free["id"], "erin")

    assert unknown_status == 404
    assert set(unknown) == {"error"}
    assert exhausted_status == 409
    assert set(exhausted) == {"error"}


def test_next_action_is_consistent_across_instances(two_process_urls, http):
    """#36/S4-S6 跨实例，TC7: 另一个实例登记的队列给出同样的下一步。"""
    (first_url, second_url), server_errors = two_process_urls
    book, seed_loan, _, _ = _stocked_book_with_a_queue(http, first_url)

    _, _, outsider = _borrow(http, second_url, book["id"], "carol")
    _, _, head = _borrow(http, second_url, book["id"], "alice")
    _, _, current = http("GET", f"{second_url}/books/{book['id']}")
    _, _, loans = http("GET", f"{second_url}/loans")

    assert outsider["code"] == head["code"] == "reservation_queue_active"
    assert outsider["next_action"] == "create_reservation"
    assert head["next_action"] == "wait_for_fulfillment"
    assert current["available_stock"] == 1
    assert [loan["id"] for loan in loans] == [seed_loan["id"]]

    server_errors.seek(0)
    assert server_errors.read() == ""
