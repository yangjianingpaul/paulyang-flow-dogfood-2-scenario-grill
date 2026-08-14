"""Ticket #27 / #26/S1-S7: per-SKU reservation queue with idempotent ordering."""

import re


RESERVATION_FIELDS = {"id", "book_id", "holder", "status", "position"}


def _book(http, base_url, title, initial_stock):
    _, _, body = http(
        "POST",
        f"{base_url}/books",
        {"title": title, "initial_stock": initial_stock},
    )
    return body


def _exhausted_book(http, base_url, title="三体-预约队列"):
    """#26 前置条件：库存 1 的 SKU 被 seed Loan 耗尽。"""
    book = _book(http, base_url, title, 1)
    http("POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "seed-borrower"})
    return book


def _reserve(http, base_url, book_id, holder):
    return http("POST", f"{base_url}/reservations", {"book_id": book_id, "holder": holder})


def test_s1_reserving_an_available_sku_is_rejected_as_directly_borrowable(base_url, http):
    """#26/S1, DEC2/TC4: 有库存时预约失败，并提示该 SKU 可直接借阅。"""
    book = _book(http, base_url, "有库存空队列", 1)

    status, _, body = _reserve(http, base_url, book["id"], "alice")

    assert status == 409
    assert isinstance(body["error"], str) and body["error"]
    assert "borrow" in body["error"].lower()
    assert set(body) == {"error"}


def test_s2_a_rejected_reservation_leaves_the_queue_empty(base_url, http):
    """#26/S2, DEC8/TC5: S1 没有创建预约，队列为空数组。"""
    book = _book(http, base_url, "有库存空队列", 1)
    _reserve(http, base_url, book["id"], "alice")

    status, headers, body = http("GET", f"{base_url}/books/{book['id']}/reservations")

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body == []


def test_s3_a_rejected_reservation_creates_no_loan(base_url, http):
    """#26/S3, DEC2/TC4: S1 没有自动借出，只剩 seed Loan。"""
    book = _exhausted_book(http, base_url)
    available = _book(http, base_url, "有库存空队列", 1)
    _reserve(http, base_url, available["id"], "alice")

    _, _, loans = http("GET", f"{base_url}/loans")

    assert [loan["borrower"] for loan in loans] == ["seed-borrower"]
    assert not [loan for loan in loans if loan["borrower"] == "alice"]


def test_s1_rejection_does_not_consume_the_available_stock(base_url, http):
    """#26/S1 的不变量，TC4: 失败的登记不改变 Book。"""
    book = _book(http, base_url, "有库存空队列", 1)

    _reserve(http, base_url, book["id"], "alice")

    _, _, after = http("GET", f"{base_url}/books/{book['id']}")
    assert after["available_stock"] == 1


def test_s4_reserving_an_exhausted_sku_returns_a_stable_waiting_reservation(base_url, http):
    """#26/S4, DEC1/DEC10/TC2: 成功、稳定 ID、holder=alice、等待且 position=1。"""
    book = _exhausted_book(http, base_url)

    status, headers, body = _reserve(http, base_url, book["id"], "alice")

    assert status == 201
    assert headers["Content-Type"] == "application/json"
    assert isinstance(body["id"], str) and body["id"]
    assert body["book_id"] == book["id"]
    assert body["holder"] == "alice"
    assert body["status"] == "waiting"
    assert body["position"] == 1
    assert set(body) == RESERVATION_FIELDS


def test_reservation_id_shape_is_rs_n_on_its_own_counter(base_url, http):
    """TC2: 公开 id 稳定、非空，形态 "rs_<n>"，独立于 book / loan 的计数器。"""
    first_book = _exhausted_book(http, base_url, "三体-预约队列")
    second_book = _exhausted_book(http, base_url, "球状闪电-预约队列")

    _, _, first = _reserve(http, base_url, first_book["id"], "alice")
    _, _, second = _reserve(http, base_url, second_book["id"], "alice")

    assert re.fullmatch(r"rs_\d+", first["id"])
    assert first["id"] == "rs_1"
    assert second["id"] == "rs_2"


def test_holder_is_a_free_string_kept_verbatim(base_url, http):
    """DEC7: holder 是原样保存的非空自由字符串。"""
    book = _exhausted_book(http, base_url)

    _, _, body = _reserve(http, base_url, book["id"], "  Alice O'Hara  ")

    assert body["holder"] == "  Alice O'Hara  "


def test_s5_repeating_a_registration_reuses_the_same_reservation(base_url, http):
    """#26/S5, DEC9/TC3: 重复登记复用既有等待预约，位置仍为 1。"""
    book = _exhausted_book(http, base_url)
    _, _, first = _reserve(http, base_url, book["id"], "alice")

    status, _, repeated = _reserve(http, base_url, book["id"], "alice")

    assert status == 200
    assert repeated["id"] == first["id"]
    assert repeated["holder"] == "alice"
    assert repeated["status"] == "waiting"
    assert repeated["position"] == 1
    assert set(repeated) == RESERVATION_FIELDS


def test_s5_repeating_a_registration_does_not_grow_the_queue(base_url, http):
    """#26/S5 的不变量，TC3: 同一 holder 对同一 SKU 至多一条等待预约。"""
    book = _exhausted_book(http, base_url)
    _reserve(http, base_url, book["id"], "alice")
    _reserve(http, base_url, book["id"], "alice")
    _reserve(http, base_url, book["id"], "alice")

    _, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")

    assert len(queue) == 1


def test_s6_a_second_holder_is_appended_at_position_two(base_url, http):
    """#26/S6, DEC3/DEC10: 不同稳定 ID、holder=bob、等待且 position=2。"""
    book = _exhausted_book(http, base_url)
    _, _, alice = _reserve(http, base_url, book["id"], "alice")

    status, _, bob = _reserve(http, base_url, book["id"], "bob")

    assert status == 201
    assert bob["id"] != alice["id"]
    assert bob["book_id"] == book["id"]
    assert bob["holder"] == "bob"
    assert bob["status"] == "waiting"
    assert bob["position"] == 2
    assert set(bob) == RESERVATION_FIELDS


def test_s7_the_queue_lists_both_holders_in_server_confirmed_order(base_url, http):
    """#26/S7, DEC3/DEC8/TC5: 恰好两条，alice position=1、bob position=2。"""
    book = _exhausted_book(http, base_url)
    _, _, alice = _reserve(http, base_url, book["id"], "alice")
    _, _, bob = _reserve(http, base_url, book["id"], "bob")

    status, _, queue = http("GET", f"{base_url}/books/{book['id']}/reservations")

    assert status == 200
    assert len(queue) == 2
    assert queue[0] == {
        "id": alice["id"],
        "book_id": book["id"],
        "holder": "alice",
        "status": "waiting",
        "position": 1,
    }
    assert queue[1] == {
        "id": bob["id"],
        "book_id": book["id"],
        "holder": "bob",
        "status": "waiting",
        "position": 2,
    }


def test_each_sku_owns_an_independent_queue(base_url, http):
    """DEC1/TC3: 同名不同 ID 的 SKU 各有独立队列与独立位置编号。"""
    first_book = _exhausted_book(http, base_url, "三体-预约队列")
    second_book = _exhausted_book(http, base_url, "三体-预约队列")
    _reserve(http, base_url, first_book["id"], "alice")
    _, _, bob_on_second = _reserve(http, base_url, second_book["id"], "bob")

    _, _, first_queue = http("GET", f"{base_url}/books/{first_book['id']}/reservations")
    _, _, second_queue = http("GET", f"{base_url}/books/{second_book['id']}/reservations")

    assert bob_on_second["position"] == 1
    assert [item["holder"] for item in first_queue] == ["alice"]
    assert [item["holder"] for item in second_queue] == ["bob"]


def test_the_same_holder_may_wait_on_two_different_skus(base_url, http):
    """TC3 的边界：唯一性约束限定在同一 SKU 内。"""
    first_book = _exhausted_book(http, base_url, "三体-预约队列")
    second_book = _exhausted_book(http, base_url, "球状闪电-预约队列")

    _, _, first = _reserve(http, base_url, first_book["id"], "alice")
    status, _, second = _reserve(http, base_url, second_book["id"], "alice")

    assert status == 201
    assert second["id"] != first["id"]
    assert second["position"] == 1


def test_reserving_a_zero_stock_sku_succeeds(base_url, http):
    """DEC2: 允许预约的条件是 available_stock=0，与库存如何归零无关。"""
    book = _book(http, base_url, "零库存空队列", 0)

    status, _, body = _reserve(http, base_url, book["id"], "alice")

    assert status == 201
    assert body["position"] == 1


def test_queue_of_a_zero_stock_sku_without_reservations_is_empty(base_url, http):
    """#26/S2 的同形查询，TC5: 没有等待者时返回空数组。"""
    book = _book(http, base_url, "零库存空队列", 0)

    status, _, body = http("GET", f"{base_url}/books/{book['id']}/reservations")

    assert status == 200
    assert body == []


def test_reserving_an_unknown_book_is_404(base_url, http):
    """TC1/TC4: 未知 SKU 以可读错误拒绝，而非崩溃。"""
    status, _, body = http(
        "POST", f"{base_url}/reservations", {"book_id": "bk_404", "holder": "alice"}
    )

    assert status == 404
    assert isinstance(body["error"], str) and body["error"]


def test_queue_of_an_unknown_book_is_404(base_url, http):
    """TC5: 未知 SKU 的队列查询以可读错误拒绝。"""
    status, _, body = http("GET", f"{base_url}/books/bk_404/reservations")

    assert status == 404
    assert isinstance(body["error"], str) and body["error"]


def test_reserve_requires_book_id_and_holder(base_url, http):
    """TC1 的请求形状：缺字段或类型不对时以可读错误拒绝。"""
    book = _exhausted_book(http, base_url)

    for payload in (
        {"holder": "alice"},
        {"book_id": book["id"]},
        {},
        {"book_id": book["id"], "holder": ""},
        {"book_id": book["id"], "holder": 7},
    ):
        status, _, body = http("POST", f"{base_url}/reservations", payload)

        assert status == 400
        assert isinstance(body["error"], str) and body["error"]


def test_existing_loan_routes_are_unchanged(base_url, http):
    """TC1: 既有 POST /loans、return、GET /loans、GET /books/{id} 路径不变。"""
    book = _book(http, base_url, "三体", 1)

    borrow_status, _, loan = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )
    return_status, _, returned = http("POST", f"{base_url}/loans/{loan['id']}/return")
    list_status, _, loans = http("GET", f"{base_url}/loans")
    book_status, _, current = http("GET", f"{base_url}/books/{book['id']}")

    assert [borrow_status, return_status, list_status, book_status] == [201, 200, 200, 200]
    assert returned["returned_at"]
    assert [item["id"] for item in loans] == [loan["id"]]
    assert current["available_stock"] == 1


def test_queue_is_shared_across_instances(two_process_urls, http):
    """#26/S4-S7 跨实例，TC6: 两个实例读写同一份共享状态。"""
    (first_url, second_url), server_errors = two_process_urls
    book = _exhausted_book(http, first_url)

    _, _, alice = _reserve(http, first_url, book["id"], "alice")
    _, _, repeated = _reserve(http, second_url, book["id"], "alice")
    _, _, bob = _reserve(http, second_url, book["id"], "bob")
    _, _, queue = http("GET", f"{first_url}/books/{book['id']}/reservations")

    assert repeated["id"] == alice["id"]
    assert repeated["position"] == 1
    assert bob["position"] == 2
    assert [(item["id"], item["position"]) for item in queue] == [
        (alice["id"], 1),
        (bob["id"], 2),
    ]

    server_errors.seek(0)
    assert server_errors.read() == ""
