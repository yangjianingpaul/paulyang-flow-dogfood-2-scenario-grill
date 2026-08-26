"""Ticket #54 / #53/S5-S22: 馆员凭预约号把等待者移出队列，且移出只整理队列。"""

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


def _remove(http, base_url, reservation_id, **extra):
    return http(
        "POST",
        f"{base_url}/reservations/remove",
        {"reservation_id": reservation_id, **extra},
    )


def _borrow(http, base_url, book_id, borrower):
    return http("POST", f"{base_url}/loans", {"book_id": book_id, "borrower": borrower})


def _restock(http, base_url, book_id, quantity):
    return http("POST", f"{base_url}/books/{book_id}/restock", {"quantity": quantity})


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
    """#53/S1-S4：0 库存的 SKU 上排好 alice(1)、bob(2)、carol(3)。前序 S 只作 setup。"""
    book = _book(http, base_url, "Deep Work", 0)
    _, _, alice = _reserve(http, base_url, book["id"], "alice")
    _, _, bob = _reserve(http, base_url, book["id"], "bob")
    _, _, carol = _reserve(http, base_url, book["id"], "carol")
    return book["id"], alice["id"], bob["id"], carol["id"]


def test_librarian_removes_a_middle_waiter_by_reservation_id(http, base_url):
    """#53/S5, #53/S6：只凭预约号移出队中第二位，队列只少一个位置。"""
    book_id, rs_alice, rs_bob, rs_carol = _three_person_queue(http, base_url)

    status, _, body = _remove(http, base_url, rs_bob)

    assert status == 200
    assert (body["id"], body["holder"], body["book_id"]) == (rs_bob, "bob", book_id)

    queue = _queue(http, base_url, book_id)
    assert _positions(queue) == [("alice", 1), ("carol", 2)]
    assert [row["id"] for row in queue] == [rs_alice, rs_carol]
    assert "bob" not in [row["holder"] for row in queue]


def test_remove_needs_no_holder_and_validates_none(http, base_url):
    """#53/S5, DEC1：请求只给预约号；即使带上一个对不上的 holder 也不被读取。"""
    book_id, _, rs_bob, _ = _three_person_queue(http, base_url)

    status, _, body = _remove(http, base_url, rs_bob, holder="mallory")

    assert status == 200
    assert body["holder"] == "bob"
    assert _positions(_queue(http, base_url, book_id)) == [("alice", 1), ("carol", 2)]


def test_removed_waiter_requeues_at_the_tail_with_a_new_id(http, base_url):
    """#53/S7, #53/S8：移出只拿掉位置，不拿掉排队资格 (DEC2)。"""
    book_id, rs_alice, rs_bob, rs_carol = _three_person_queue(http, base_url)
    _remove(http, base_url, rs_bob)

    status, _, again = _reserve(http, base_url, book_id, "bob")

    assert status == 201
    assert again["position"] == 3
    assert again["id"] != rs_bob

    queue = _queue(http, base_url, book_id)
    assert _positions(queue) == [("alice", 1), ("carol", 2), ("bob", 3)]
    assert [row["id"] for row in queue] == [rs_alice, rs_carol, again["id"]]


def test_remove_is_not_idempotent(http, base_url):
    """#53/S9, #53/S10：拿已经不在队列里的旧号再移一次被拒，队列一条都不改 (DEC3)。"""
    book_id, _, rs_bob, _ = _three_person_queue(http, base_url)
    _remove(http, base_url, rs_bob)
    _, _, bob_again = _reserve(http, base_url, book_id, "bob")

    status, _, body = _remove(http, base_url, rs_bob)

    assert status == 404
    assert body["code"] == RESERVATION_NOT_FOUND
    assert isinstance(body["error"], str) and body["error"]

    queue = _queue(http, base_url, book_id)
    assert _positions(queue) == [("alice", 1), ("carol", 2), ("bob", 3)]
    assert queue[2]["id"] == bob_again["id"]


def test_rejected_remove_leaves_every_table_untouched(http, base_url):
    """#53/S10, TC5：被拒的移出在 rollback 之后三张表一行都不改变。"""
    book_id, _, rs_bob, _ = _three_person_queue(http, base_url)
    _restock(http, base_url, book_id, 1)

    before_queue = _queue(http, base_url, book_id)
    before_stock = _stock(http, base_url, book_id)
    before_loans = _loans(http, base_url)

    # 一条不存在的号被拒之后，队列、库存与 Loan 全部原样。
    _remove(http, base_url, "rs_999999")

    assert _queue(http, base_url, book_id) == before_queue
    assert _stock(http, base_url, book_id) == before_stock
    assert _loans(http, base_url) == before_loans

    # 一次成功移出之后再移一次，被拒的那一次同样一行都不改。
    _remove(http, base_url, rs_bob)
    after_success = _queue(http, base_url, book_id)

    _remove(http, base_url, rs_bob)

    assert _queue(http, base_url, book_id) == after_success
    assert _positions(after_success) == [("alice", 1), ("carol", 2)]
    assert _stock(http, base_url, book_id) == before_stock
    assert _loans(http, base_url) == before_loans


def test_removing_the_head_touches_neither_stock_nor_loans(http, base_url):
    """#53/S11-S15：移出队首不消耗库存、不发书，新队首自然上位 (DEC4)。"""
    book_id, rs_alice, rs_bob, rs_carol = _three_person_queue(http, base_url)
    # #53/S5, #53/S7 只作 setup：bob 被移出后重新登记落到队尾，
    # 队列因而是 alice(1)、carol(2)、bob(3)。
    _remove(http, base_url, rs_bob)
    _, _, bob_again = _reserve(http, base_url, book_id, "bob")
    rs_bob = bob_again["id"]

    _restock(http, base_url, book_id, 1)
    assert _stock(http, base_url, book_id) == 1

    status, _, body = _remove(http, base_url, rs_alice)

    assert status == 200
    assert (body["id"], body["holder"]) == (rs_alice, "alice")
    assert set(body) == {"id", "book_id", "holder"}  # 响应中不含任何 Loan

    assert _stock(http, base_url, book_id) == 1
    assert _loans(http, base_url) == []
    queue = _queue(http, base_url, book_id)
    assert _positions(queue) == [("carol", 1), ("bob", 2)]
    assert [row["id"] for row in queue] == [rs_carol, rs_bob]


def test_full_removal_scenario_s5_to_s22(http, base_url):
    """#53/S5-S22：整段重放 —— 三次成功的移出没有产生任何 Loan。"""
    book_id, rs_alice, rs_bob, _ = _three_person_queue(http, base_url)

    remove_status, _, removed = _remove(http, base_url, rs_bob)              # S5
    assert remove_status == 200
    assert removed["id"] == rs_bob
    assert _positions(_queue(http, base_url, book_id)) == [                  # S6
        ("alice", 1),
        ("carol", 2),
    ]

    _, _, bob_2 = _reserve(http, base_url, book_id, "bob")                   # S7
    assert bob_2["position"] == 3 and bob_2["id"] != rs_bob
    assert _positions(_queue(http, base_url, book_id)) == [                  # S8
        ("alice", 1),
        ("carol", 2),
        ("bob", 3),
    ]

    stale_status, _, stale = _remove(http, base_url, rs_bob)                 # S9
    assert stale_status == 404 and stale["code"] == RESERVATION_NOT_FOUND
    assert _positions(_queue(http, base_url, book_id)) == [                  # S10
        ("alice", 1),
        ("carol", 2),
        ("bob", 3),
    ]

    _restock(http, base_url, book_id, 1)                                     # S11
    assert _stock(http, base_url, book_id) == 1

    head_status, _, head = _remove(http, base_url, rs_alice)                 # S12
    assert head_status == 200 and head["holder"] == "alice"
    assert _stock(http, base_url, book_id) == 1                              # S13
    assert _loans(http, base_url) == []                                      # S14
    assert _positions(_queue(http, base_url, book_id)) == [                  # S15
        ("carol", 1),
        ("bob", 2),
    ]

    fulfill_status, _, fulfilled = _fulfill(http, base_url, book_id)         # S16
    assert fulfill_status == 201
    assert fulfilled["reservation"]["holder"] == "carol"
    assert fulfilled["loan"]["borrower"] == "carol"
    assert fulfilled["loan"]["returned_at"] is None

    _restock(http, base_url, book_id, 1)                                     # S17
    assert _stock(http, base_url, book_id) == 1

    dave_status, _, dave_rejected = _borrow(http, base_url, book_id, "dave")  # S18
    assert dave_status == 409
    assert dave_rejected["code"] == RESERVATION_QUEUE_ACTIVE
    assert dave_rejected["next_action"] == CREATE_RESERVATION

    last_status, _, last = _remove(http, base_url, bob_2["id"])              # S19
    assert last_status == 200
    assert (last["id"], last["holder"]) == (bob_2["id"], "bob")
    assert _queue(http, base_url, book_id) == []                             # S20

    dave_status, _, dave = _borrow(http, base_url, book_id, "dave")          # S21
    assert dave_status == 201
    assert dave["borrower"] == "dave" and dave["returned_at"] is None

    assert [loan["borrower"] for loan in _loans(http, base_url)] == [        # S22
        "carol",
        "dave",
    ]


def test_remove_rejects_a_missing_reservation_id(http, base_url):
    """TC1：唯一字段 reservation_id 必须是非空字符串。"""
    _three_person_queue(http, base_url)

    status, _, body = http("POST", f"{base_url}/reservations/remove", {})

    assert status == 400
    assert isinstance(body["error"], str) and body["error"]
