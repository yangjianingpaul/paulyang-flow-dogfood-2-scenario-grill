"""Ticket #49 / #48/S5-S28: 预约者本人取消自己的等待预约，并收回单人处置的一次性放行。"""

RESERVATION_NOT_FOUND = "reservation_not_found"
RESERVATION_QUEUE_ACTIVE = "reservation_queue_active"
CREATE_RESERVATION = "create_reservation"


def _book(http, base_url, title, initial_stock):
    _, _, body = http(
        "POST",
        f"{base_url}/books",
        {"title": title, "initial_stock": initial_stock},
    )
    return body


def _reserve(http, base_url, book_id, holder):
    return http(
        "POST",
        f"{base_url}/reservations",
        {"book_id": book_id, "holder": holder},
    )


def _cancel(http, base_url, reservation_id, holder):
    return http(
        "POST",
        f"{base_url}/reservations/cancel",
        {"reservation_id": reservation_id, "holder": holder},
    )


def _borrow(http, base_url, book_id, borrower):
    return http("POST", f"{base_url}/loans", {"book_id": book_id, "borrower": borrower})


def _restock(http, base_url, book_id, quantity):
    return http("POST", f"{base_url}/books/{book_id}/restock", {"quantity": quantity})


def _defer(http, base_url, book_id):
    return http("POST", f"{base_url}/books/{book_id}/reservations/defer")


def _fulfill(http, base_url, book_id):
    return http("POST", f"{base_url}/books/{book_id}/reservations/fulfill")


def _queue(http, base_url, book_id):
    _, _, body = http("GET", f"{base_url}/books/{book_id}/reservations")
    return body


def _stock(http, base_url, book_id):
    _, _, body = http("GET", f"{base_url}/books/{book_id}")
    return body["available_stock"]


def _loans(http, base_url):
    _, _, body = http("GET", f"{base_url}/loans")
    return body


def _positions(queue):
    return [(row["holder"], row["position"]) for row in queue]


def _three_person_queue(http, base_url):
    """#48/S1-S4：0 库存的 SKU 上排好 alice(1)、bob(2)、carol(3)。前序 S 只作 setup。"""
    book = _book(http, base_url, "Dune", 0)
    _, _, alice = _reserve(http, base_url, book["id"], "alice")
    _, _, bob = _reserve(http, base_url, book["id"], "bob")
    _, _, carol = _reserve(http, base_url, book["id"], "carol")
    return book["id"], alice["id"], bob["id"], carol["id"]


def test_foreign_holder_cannot_cancel_and_queue_stays_intact(http, base_url):
    """#48/S5, #48/S6：冒用者拿着别人的预约 ID 取消不成立，队列一条未动。"""
    book_id, rs_alice, _, _ = _three_person_queue(http, base_url)

    status, _, body = _cancel(http, base_url, rs_alice, "mallory")

    assert status == 404
    assert body["code"] == RESERVATION_NOT_FOUND
    assert isinstance(body["error"], str) and body["error"]
    # 冒用者不能从响应里读出这个 ID 确实存在、只是属于别人 (#48/S5 软示例, DEC4)。
    assert "alice" not in str(body)

    queue = _queue(http, base_url, book_id)
    assert _positions(queue) == [("alice", 1), ("bob", 2), ("carol", 3)]
    assert queue[0]["id"] == rs_alice


def test_holder_cancels_own_reservation_and_queue_converges(http, base_url):
    """#48/S7, #48/S8：本人取消成功，其余等待者的 position 自然收敛。"""
    book_id, rs_alice, rs_bob, rs_carol = _three_person_queue(http, base_url)

    status, _, _ = _cancel(http, base_url, rs_alice, "alice")
    assert 200 <= status < 300

    queue = _queue(http, base_url, book_id)
    assert _positions(queue) == [("bob", 1), ("carol", 2)]
    assert [row["id"] for row in queue] == [rs_bob, rs_carol]


def test_cancel_is_not_idempotent(http, base_url):
    """#48/S9, #48/S10：同一条预约第二次取消明确失败，队列不变 (DEC3)。"""
    book_id, rs_alice, _, _ = _three_person_queue(http, base_url)
    _cancel(http, base_url, rs_alice, "alice")

    status, _, body = _cancel(http, base_url, rs_alice, "alice")

    assert status == 404
    assert body["code"] == RESERVATION_NOT_FOUND
    assert isinstance(body["error"], str) and body["error"]
    assert _positions(_queue(http, base_url, book_id)) == [("bob", 1), ("carol", 2)]


def test_both_rejections_are_indistinguishable(http, base_url):
    """DEC4：冒用与取消一条不存在的预约，状态码、code 与响应字段集合完全相同。"""
    _, rs_alice, _, _ = _three_person_queue(http, base_url)

    impostor_status, _, impostor_body = _cancel(http, base_url, rs_alice, "mallory")
    missing_status, _, missing_body = _cancel(http, base_url, "rs_999999", "alice")

    assert impostor_status == missing_status == 404
    assert set(impostor_body) == set(missing_body)
    assert impostor_body["code"] == missing_body["code"] == RESERVATION_NOT_FOUND


def test_cancel_leaves_every_table_untouched(http, base_url):
    """#48/S6, #48/S10：被拒的取消不改动预约、库存与 Loan 中的任何一行 (TC3, TC7)。"""
    book_id, rs_alice, _, _ = _three_person_queue(http, base_url)
    _restock(http, base_url, book_id, 1)

    before_queue = _queue(http, base_url, book_id)
    before_stock = _stock(http, base_url, book_id)
    before_loans = _loans(http, base_url)

    _cancel(http, base_url, rs_alice, "mallory")
    _cancel(http, base_url, "rs_999999", "alice")

    assert _queue(http, base_url, book_id) == before_queue
    assert _stock(http, base_url, book_id) == before_stock
    assert _loans(http, base_url) == before_loans


def test_recreated_reservation_starts_from_the_tail_with_a_new_id(http, base_url):
    """#48/S11, #48/S12：取消即放弃原位置，重新登记按新读者对待 (DEC2)。"""
    book_id, rs_alice_1, _, _ = _three_person_queue(http, base_url)
    _cancel(http, base_url, rs_alice_1, "alice")

    status, _, again = _reserve(http, base_url, book_id, "alice")

    assert 200 <= status < 300
    assert again["position"] == 3
    assert again["id"] != rs_alice_1
    assert _positions(_queue(http, base_url, book_id)) == [
        ("bob", 1),
        ("carol", 2),
        ("alice", 3),
    ]


def test_cancel_and_requeue_revokes_the_solo_defer_release(http, base_url):
    """#48/S13-S27：被处置者取消并重新排队，收回此前那次一次性放行 (DEC5, TC4)。"""
    book_id, rs_alice_1, _, rs_carol = _three_person_queue(http, base_url)
    _cancel(http, base_url, rs_alice_1, "alice")
    _, _, alice_2 = _reserve(http, base_url, book_id, "alice")  # S11
    _restock(http, base_url, book_id, 1)                        # S13
    _fulfill(http, base_url, book_id)                           # S14 → bob
    _cancel(http, base_url, rs_carol, "carol")                  # S15
    _restock(http, base_url, book_id, 1)                        # S17

    # S16, S17：单人锁死局面成立 —— 队列只剩 alice，架上有一本。
    assert _positions(_queue(http, base_url, book_id)) == [("alice", 1)]
    assert _stock(http, base_url, book_id) == 1

    deferred_status, _, deferred = _defer(http, base_url, book_id)  # S18
    assert 200 <= deferred_status < 300
    assert (deferred["holder"], deferred["status"], deferred["position"]) == (
        "alice",
        "waiting",
        1,
    )

    cancel_status, _, _ = _cancel(http, base_url, alice_2["id"], "alice")  # S19
    assert 200 <= cancel_status < 300
    assert _queue(http, base_url, book_id) == []                          # S20

    dave_status, _, dave = _borrow(http, base_url, book_id, "dave")       # S21
    assert 200 <= dave_status < 300
    assert dave["borrower"] == "dave"

    _, _, alice_3 = _reserve(http, base_url, book_id, "alice")            # S22
    assert alice_3["position"] == 1
    assert alice_3["id"] != alice_2["id"]
    _restock(http, base_url, book_id, 1)                                  # S23

    # S24：同样的单人队列、同样的库存，放行已被取消并重新排队收回。
    erin_status, _, erin_rejected = _borrow(http, base_url, book_id, "erin")
    assert erin_status == 409
    assert erin_rejected["code"] == RESERVATION_QUEUE_ACTIVE
    assert erin_rejected["next_action"] == CREATE_RESERVATION
    assert _stock(http, base_url, book_id) == 1                           # S25

    # S26, S27：对照组 —— 重新处置一次，放行机制本身仍然有效。
    _defer(http, base_url, book_id)
    erin_status, _, erin = _borrow(http, base_url, book_id, "erin")
    assert 200 <= erin_status < 300
    assert erin["borrower"] == "erin"

    # S28：取消从未发出过一本书。
    assert [loan["borrower"] for loan in _loans(http, base_url)] == [
        "bob",
        "dave",
        "erin",
    ]
