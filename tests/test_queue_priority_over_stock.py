"""Ticket #38 / #36/S7-S25: 队列非空时队列优先于库存，直到队列被兑现清空。"""


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


def _stocked_book_with_a_queue(http, base_url):
    """#36 前置条件 + S1：库存 1、队列 alice(1)/bob(2)、seed Loan 未归还。"""
    book = _book(http, base_url, "三体-队列优先", 1)
    _, _, seed_loan = _borrow(http, base_url, book["id"], "seed-borrower")
    _, _, alice = _reserve(http, base_url, book["id"], "alice")
    _, _, bob = _reserve(http, base_url, book["id"], "bob")
    _restock(http, base_url, book["id"], 1)
    return book, seed_loan, alice, bob


def _queue_of_three(http, base_url):
    """#36/S7 之后：carol 在有库存时排到队尾，队列 alice/bob/carol。"""
    book, seed_loan, alice, bob = _stocked_book_with_a_queue(http, base_url)
    _, _, carol = _reserve(http, base_url, book["id"], "carol")
    return book, seed_loan, alice, bob, carol


def _restocked_to_three(http, base_url):
    """#36/S11 + S14 之后：alice 已被兑现，库存 3、队列 bob(1)/carol(2)。"""
    book, seed_loan, alice, bob, carol = _queue_of_three(http, base_url)
    _, _, alice_fulfillment = _fulfill(http, base_url, book["id"])
    _restock(http, base_url, book["id"], 3)
    return book, seed_loan, alice, bob, carol, alice_fulfillment


def _emptied_queue(http, base_url):
    """#36/S17 + S18 之后：连续兑现 bob 与 carol，队列清空、库存 1。"""
    book, seed_loan, alice, bob, carol, alice_fulfillment = _restocked_to_three(
        http, base_url
    )
    _, _, bob_fulfillment = _fulfill(http, base_url, book["id"])
    _, _, carol_fulfillment = _fulfill(http, base_url, book["id"])
    return (
        book,
        seed_loan,
        (alice, alice_fulfillment),
        (bob, bob_fulfillment),
        (carol, carol_fulfillment),
    )


def test_s7_a_new_holder_joins_the_tail_while_stock_is_available(base_url, http):
    """#36/S7, TC4/DEC6: 队列非空时有库存不再阻碍登记，carol 排到 position 3。"""
    book, _, alice, bob = _stocked_book_with_a_queue(http, base_url)

    status, headers, body = _reserve(http, base_url, book["id"], "carol")

    assert status == 201
    assert headers["Content-Type"] == "application/json"
    assert isinstance(body["id"], str) and body["id"]
    assert body["id"] not in {alice["id"], bob["id"]}
    assert body["book_id"] == book["id"]
    assert body["holder"] == "carol"
    assert body["status"] == "waiting"
    assert body["position"] == 3
    assert set(body) == {"id", "book_id", "holder", "status", "position"}


def test_s8_the_queue_lists_three_holders_in_confirmed_order(base_url, http):
    """#36/S8, TC6: 恰好三条，alice(1)、bob(2)、carol(3)。"""
    book, _, alice, bob, carol = _queue_of_three(http, base_url)

    status, _, body = http("GET", f"{base_url}/books/{book['id']}/reservations")

    assert status == 200
    assert [(item["id"], item["holder"], item["position"]) for item in body] == [
        (alice["id"], "alice", 1),
        (bob["id"], "bob", 2),
        (carol["id"], "carol", 3),
    ]


def test_s9_repeating_carols_registration_reuses_the_same_reservation(base_url, http):
    """#36/S9, TC4/DEC1: 同一状态下重复登记复用原记录与原 position。"""
    book, _, _, _, carol = _queue_of_three(http, base_url)

    status, _, body = _reserve(http, base_url, book["id"], "carol")

    assert status == 200
    assert body["id"] == carol["id"]
    assert body["position"] == 3


def test_s9_repeating_carols_registration_does_not_grow_the_queue(base_url, http):
    """#36/S9, TC3: 队列长度不增加，没有第二条 carol 预约。"""
    book, _, _, _, carol = _queue_of_three(http, base_url)

    _reserve(http, base_url, book["id"], "carol")

    queue = _queue(http, base_url, book["id"])
    assert len(queue) == 3
    assert [item["holder"] for item in queue] == ["alice", "bob", "carol"]
    assert [item["id"] for item in queue].count(carol["id"]) == 1


def test_s10_registering_consumes_no_stock_and_fulfils_nobody(base_url, http):
    """#36/S10, TC3: 两次登记之后库存仍为 1，且没有自动兑现任何等待者。"""
    book, seed_loan, _, _, _ = _queue_of_three(http, base_url)
    _reserve(http, base_url, book["id"], "carol")

    assert _stock(http, base_url, book["id"]) == 1
    assert [loan["id"] for loan in _loans(http, base_url)] == [seed_loan["id"]]


def test_s11_the_librarian_fulfils_the_head_of_the_queue(base_url, http):
    """#36/S11, TC7: 被兑现的是队首 alice，并产生 borrower=alice 的同 SKU Loan。"""
    book, _, alice, _, _ = _queue_of_three(http, base_url)

    status, _, body = _fulfill(http, base_url, book["id"])

    assert status == 201
    assert body["reservation"]["id"] == alice["id"]
    assert body["reservation"]["holder"] == "alice"
    assert body["loan"]["book_id"] == book["id"]
    assert body["loan"]["borrower"] == "alice"
    assert body["loan"]["id"] in {loan["id"] for loan in _loans(http, base_url)}


def test_s12_fulfilment_consumes_one_copy(base_url, http):
    """#36/S12, TC3: 兑现之后 available_stock = 0。"""
    book, _, _, _, _ = _queue_of_three(http, base_url)

    _fulfill(http, base_url, book["id"])

    assert _stock(http, base_url, book["id"]) == 0


def test_s13_the_fulfilled_head_leaves_the_queue_and_positions_move_up(base_url, http):
    """#36/S13, TC6: 剩 bob(1)、carol(2)，alice 不再在等待队列中。"""
    book, _, alice, bob, carol = _queue_of_three(http, base_url)

    _fulfill(http, base_url, book["id"])

    queue = _queue(http, base_url, book["id"])
    assert [(item["id"], item["holder"], item["position"]) for item in queue] == [
        (bob["id"], "bob", 1),
        (carol["id"], "carol", 2),
    ]
    assert alice["id"] not in {item["id"] for item in queue}


def test_s14_restocking_beyond_the_queue_length_changes_nothing_else(base_url, http):
    """#36/S14, TC3: 库存补到 3，等待队列仍是 2 人。"""
    book, _, _, bob, carol = _queue_of_three(http, base_url)
    _fulfill(http, base_url, book["id"])

    status, _, body = _restock(http, base_url, book["id"], 3)

    assert status == 200
    assert body["available_stock"] == 3
    assert [item["holder"] for item in _queue(http, base_url, book["id"])] == [
        "bob",
        "carol",
    ]


def test_s15_a_non_head_waiter_is_told_to_wait_even_with_surplus_stock(base_url, http):
    """#36/S15, TC1/TC5: 队列第 2 位的 carol 得到与队首同形的 409 与下一步。"""
    book, _, _, _, _, _ = _restocked_to_three(http, base_url)

    status, headers, body = _borrow(http, base_url, book["id"], "carol")

    assert status == 409
    assert headers["Content-Type"] == "application/json"
    assert isinstance(body["error"], str) and body["error"]
    assert body["code"] == "reservation_queue_active"
    assert body["next_action"] == "wait_for_fulfillment"
    assert set(body) == {"error", "code", "next_action"}


def test_s16_surplus_stock_is_not_opened_to_direct_borrowing(base_url, http):
    """#36/S16, TC3: 库存多于等待人数，被拒的借阅仍未消耗任何一本。"""
    book, _, _, _, _, _ = _restocked_to_three(http, base_url)

    _borrow(http, base_url, book["id"], "carol")

    assert _stock(http, base_url, book["id"]) == 3
    assert [item["holder"] for item in _queue(http, base_url, book["id"])] == [
        "bob",
        "carol",
    ]


def test_s17_the_second_fulfilment_hands_the_book_to_bob(base_url, http):
    """#36/S17, TC7: 第二次兑现的是 bob，并产生 borrower=bob 的同 SKU Loan。"""
    book, _, _, bob, _, _ = _restocked_to_three(http, base_url)

    status, _, body = _fulfill(http, base_url, book["id"])

    assert status == 201
    assert body["reservation"]["id"] == bob["id"]
    assert body["reservation"]["holder"] == "bob"
    assert body["loan"]["book_id"] == book["id"]
    assert body["loan"]["borrower"] == "bob"


def test_s18_the_third_fulfilment_hands_the_book_to_carol(base_url, http):
    """#36/S18, TC7: 第三次兑现的是 carol，队列随之清空。"""
    book, _, _, _, carol, _ = _restocked_to_three(http, base_url)
    _fulfill(http, base_url, book["id"])

    status, _, body = _fulfill(http, base_url, book["id"])

    assert status == 201
    assert body["reservation"]["id"] == carol["id"]
    assert body["reservation"]["holder"] == "carol"
    assert body["loan"]["book_id"] == book["id"]
    assert body["loan"]["borrower"] == "carol"


def test_s19_the_queue_is_empty_once_every_waiter_is_fulfilled(base_url, http):
    """#36/S19, TC6: 三次兑现之后队列为空数组。"""
    book, _, _, _, _ = _emptied_queue(http, base_url)

    status, _, body = http("GET", f"{base_url}/books/{book['id']}/reservations")

    assert status == 200
    assert body == []


def test_s20_two_of_the_three_restocked_copies_were_consumed(base_url, http):
    """#36/S20, TC3: 清空之后 available_stock = 1。"""
    book, _, _, _, _ = _emptied_queue(http, base_url)

    assert _stock(http, base_url, book["id"]) == 1


def test_s21_an_empty_queue_restores_the_original_reservation_admission(base_url, http):
    """#36/S21, TC4/DEC6 保留侧: 队列为空且有库存时 erin 的登记仍被拒。"""
    book, _, _, _, _ = _emptied_queue(http, base_url)

    status, _, body = _reserve(http, base_url, book["id"], "erin")

    assert status == 409
    assert isinstance(body["error"], str) and body["error"]
    assert "borrow" in body["error"].lower()
    assert set(body) == {"error"}


def test_s22_the_rejected_registration_created_nothing(base_url, http):
    """#36/S22, TC6/DEC6 保留侧: 队列仍为空数组，S21 没有创建任何预约。"""
    book, _, _, _, _ = _emptied_queue(http, base_url)

    _reserve(http, base_url, book["id"], "erin")

    assert _queue(http, base_url, book["id"]) == []


def test_s23_direct_borrowing_reopens_once_the_queue_is_empty(base_url, http):
    """#36/S23, TC5/DEC2: dave 借阅成功，响应不再带 reservation_queue_active。"""
    book, _, _, _, _ = _emptied_queue(http, base_url)

    status, _, body = _borrow(http, base_url, book["id"], "dave")

    assert status == 201
    assert body["book_id"] == book["id"]
    assert body["borrower"] == "dave"
    assert body.get("code") != "reservation_queue_active"


def test_s24_daves_loan_consumed_the_last_copy(base_url, http):
    """#36/S24, TC3: 借出之后 available_stock = 0。"""
    book, _, _, _, _ = _emptied_queue(http, base_url)

    _borrow(http, base_url, book["id"], "dave")

    assert _stock(http, base_url, book["id"]) == 0


def test_s25_exactly_five_loans_exist_at_the_end_of_the_scenario(base_url, http):
    """#36/S25, TC7: 五条 Loan，无第二条 carol Loan，也没有 erin 的 Loan。"""
    book, _, _, _, _ = _emptied_queue(http, base_url)
    _reserve(http, base_url, book["id"], "erin")
    _borrow(http, base_url, book["id"], "dave")

    loans = _loans(http, base_url)

    assert [loan["borrower"] for loan in loans] == [
        "seed-borrower",
        "alice",
        "bob",
        "carol",
        "dave",
    ]
    assert {loan["book_id"] for loan in loans} == {book["id"]}


def test_an_empty_queue_still_rejects_a_reservation_on_a_stocked_sku(base_url, http):
    """TC4/DEC6 保留侧: 取代只在队列非空时生效，#26/DEC2 在空队列上原样成立。"""
    book = _book(http, base_url, "有库存空队列", 1)

    status, _, body = _reserve(http, base_url, book["id"], "alice")

    assert status == 409
    assert "borrow" in body["error"].lower()
    assert _queue(http, base_url, book["id"]) == []
    assert _stock(http, base_url, book["id"]) == 1


def test_the_queue_of_another_sku_does_not_open_this_ones_admission(base_url, http):
    """TC2/TC4: 准入只看本 SKU 的等待队列，队列成员身份不跨 SKU。"""
    queued, _, _, _ = _stocked_book_with_a_queue(http, base_url)
    free = _book(http, base_url, "有库存空队列", 1)

    accepted_status, _, accepted = _reserve(http, base_url, queued["id"], "carol")
    rejected_status, _, rejected = _reserve(http, base_url, free["id"], "carol")

    assert accepted_status == 201
    assert accepted["position"] == 3
    assert rejected_status == 409
    assert "borrow" in rejected["error"].lower()


def test_admission_reopens_and_closes_with_the_queue(base_url, http):
    """TC4/DEC6: 取代的条件是队列非空本身 —— 清空后准入回到原有语义。"""
    book, _, _, _, _ = _queue_of_three(http, base_url)

    while _queue(http, base_url, book["id"]):
        if _stock(http, base_url, book["id"]) == 0:
            _restock(http, base_url, book["id"], 1)
        _fulfill(http, base_url, book["id"])
    _restock(http, base_url, book["id"], 1)

    status, _, body = _reserve(http, base_url, book["id"], "erin")
    assert status == 409
    assert "borrow" in body["error"].lower()


def test_a_zero_stock_sku_with_an_empty_queue_still_accepts_a_reservation(
    base_url, http
):
    """TC4/DEC6 保留侧: 队列为空且库存为零时登记仍然成功。"""
    book = _book(http, base_url, "零库存空队列", 0)

    status, _, body = _reserve(http, base_url, book["id"], "alice")

    assert status == 201
    assert body["position"] == 1


def test_the_queue_priority_admission_is_shared_across_instances(
    two_process_urls, http
):
    """#36/S7-S18 跨实例，TC7: 两个实例在同一份共享 SQLite 上推进同一个队列。"""
    (first_url, second_url), server_errors = two_process_urls
    book, seed_loan, alice, bob = _stocked_book_with_a_queue(http, first_url)

    status, _, carol = _reserve(http, second_url, book["id"], "carol")
    assert status == 201
    assert carol["position"] == 3
    assert [item["holder"] for item in _queue(http, first_url, book["id"])] == [
        "alice",
        "bob",
        "carol",
    ]

    _, _, first_fulfillment = _fulfill(http, second_url, book["id"])
    _restock(http, first_url, book["id"], 3)
    _, _, second_fulfillment = _fulfill(http, first_url, book["id"])
    _, _, third_fulfillment = _fulfill(http, second_url, book["id"])

    assert [
        first_fulfillment["reservation"]["id"],
        second_fulfillment["reservation"]["id"],
        third_fulfillment["reservation"]["id"],
    ] == [alice["id"], bob["id"], carol["id"]]
    assert _queue(http, first_url, book["id"]) == []
    assert _stock(http, second_url, book["id"]) == 1
    assert [loan["borrower"] for loan in _loans(http, first_url)] == [
        "seed-borrower",
        "alice",
        "bob",
        "carol",
    ]
    assert seed_loan["borrower"] == "seed-borrower"

    server_errors.seek(0)
    assert server_errors.read() == ""
