"""Ticket #44 / #42/S9-S16: 队列只剩一人时的处置放行这一本，且放行是一次性的。"""

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


def _solo_locked_book(http, base_url):
    """#42/S1-S9：走完前序步骤，停在「队列只剩乙一人、可用库存 1」的单人锁死局面。

    前序 S 只作 setup：S1-S4 建立乙、丙的队列并进货，S6 把乙处置到队尾，
    S8 把这一本兑现给丙，S9 再进 1 本 —— 此时队列恰好只剩乙一个人。
    """
    book = _book(http, base_url, "深入理解计算机系统", 0)
    _, _, yi = _reserve(http, base_url, book["id"], "乙")
    _reserve(http, base_url, book["id"], "丙")
    _restock(http, base_url, book["id"], 1)
    _defer(http, base_url, book["id"])
    _fulfill(http, base_url, book["id"])
    _restock(http, base_url, book["id"], 1)
    return book, yi


def test_s9_restocking_recreates_the_lock_with_a_single_waiter(base_url, http):
    """#42/S9, #42/TC2: 补货成功且可用库存为 1，单人锁死局面成立。"""
    book = _book(http, base_url, "深入理解计算机系统", 0)
    _, _, yi = _reserve(http, base_url, book["id"], "乙")
    _reserve(http, base_url, book["id"], "丙")
    _restock(http, base_url, book["id"], 1)
    _defer(http, base_url, book["id"])
    _fulfill(http, base_url, book["id"])

    status, _, body = _restock(http, base_url, book["id"], 1)

    assert 200 <= status < 300
    assert body["available_stock"] == 1
    assert [(item["id"], item["position"]) for item in _queue(http, base_url, book["id"])] == [
        (yi["id"], 1)
    ]


def test_s10_deferring_the_only_waiter_keeps_their_position(base_url, http):
    """#42/S10, DEC3/#42/TC1/#42/TC5: 队列里只有乙，处置成功但位置不变。"""
    book, yi = _solo_locked_book(http, base_url)

    status, headers, body = _defer(http, base_url, book["id"])

    assert 200 <= status < 300
    assert headers["Content-Type"] == "application/json"
    assert body["id"] == yi["id"]
    assert body["book_id"] == book["id"]
    assert body["holder"] == "乙"
    assert body["status"] == "waiting"
    assert body["position"] == 1


def test_s10_deferring_the_only_waiter_hands_out_no_loan(base_url, http):
    """#42/S10, DEC4/#42/TC4: 这一步不发书 —— 无 Loan，可用库存一路保持到 S11。"""
    book, _ = _solo_locked_book(http, base_url)

    before = _loans(http, base_url)

    _, _, body = _defer(http, base_url, book["id"])

    assert set(body) == RESERVATION_FIELDS
    # 处置没有产生任何新的借出记录：S8 兑现给丙的那一条是前序 setup 留下的。
    assert _loans(http, base_url) == before == [
        {**before[0], "borrower": "丙"}
    ]
    assert _stock(http, base_url, book["id"]) == 1


def test_s11_an_outsider_borrows_the_released_copy(base_url, http):
    """#42/S11, DEC6/#42/TC6: 放行之后，队首以外的丁的普通借阅拿到这一本。"""
    book, _ = _solo_locked_book(http, base_url)
    _defer(http, base_url, book["id"])

    status, _, body = _borrow(http, base_url, book["id"], "丁")

    assert 200 <= status < 300
    assert body["borrower"] == "丁"
    assert body["book_id"] == book["id"]
    assert set(body) == LOAN_FIELDS
    assert _stock(http, base_url, book["id"]) == 0


def test_s12_the_deferred_reader_loses_nothing(base_url, http):
    """#42/S12, DEC1/DEC3/#42/TC3/#42/TC8: 借走之后乙一个人都没少，仍是 position 1。"""
    book, yi = _solo_locked_book(http, base_url)
    _defer(http, base_url, book["id"])
    _borrow(http, base_url, book["id"], "丁")

    queue = _queue(http, base_url, book["id"])

    assert [(item["id"], item["holder"], item["position"]) for item in queue] == [
        (yi["id"], "乙", 1)
    ]
    assert {item["status"] for item in queue} == {"waiting"}
    assert all(set(item) == RESERVATION_FIELDS for item in queue)


def test_s13_restocking_after_the_release_was_consumed(base_url, http):
    """#42/S13, #42/TC2: 补货成功，可用库存回到 1。"""
    book, _ = _solo_locked_book(http, base_url)
    _defer(http, base_url, book["id"])
    _borrow(http, base_url, book["id"], "丁")

    status, _, body = _restock(http, base_url, book["id"], 1)

    assert 200 <= status < 300
    assert body["available_stock"] == 1


def test_s14_the_release_is_one_shot(base_url, http):
    """#42/S14, DEC3/DEC6/#42/TC6: 放行已被 S11 消耗，同一调用回到被拒。"""
    book, _ = _solo_locked_book(http, base_url)
    _defer(http, base_url, book["id"])
    _borrow(http, base_url, book["id"], "丁")
    _restock(http, base_url, book["id"], 1)

    status, _, body = _borrow(http, base_url, book["id"], "丁")

    assert 400 <= status < 500
    assert "id" not in body
    assert _stock(http, base_url, book["id"]) == 1
    assert [loan["borrower"] for loan in _loans(http, base_url)] == ["丙", "丁"]


def test_s15_deferring_again_releases_the_copy_again(base_url, http):
    """#42/S15, DEC3/#42/TC1: 馆员可以再处置一次使这一本重新放行，位置仍为 1。"""
    book, yi = _solo_locked_book(http, base_url)
    _defer(http, base_url, book["id"])
    _borrow(http, base_url, book["id"], "丁")
    _restock(http, base_url, book["id"], 1)

    status, _, body = _defer(http, base_url, book["id"])

    assert 200 <= status < 300
    assert body["id"] == yi["id"]
    assert body["holder"] == "乙"
    assert body["status"] == "waiting"
    assert body["position"] == 1
    assert set(body) == RESERVATION_FIELDS
    assert [loan["borrower"] for loan in _loans(http, base_url)] == ["丙", "丁"]
    assert _stock(http, base_url, book["id"]) == 1


def test_s15_the_second_release_lets_an_outsider_borrow_again(base_url, http):
    """#42/S15, DEC3/#42/TC6: 重新放行确实生效，丁又能拿到这一本。"""
    book, _ = _solo_locked_book(http, base_url)
    _defer(http, base_url, book["id"])
    _borrow(http, base_url, book["id"], "丁")
    _restock(http, base_url, book["id"], 1)
    _defer(http, base_url, book["id"])

    status, _, body = _borrow(http, base_url, book["id"], "丁")

    assert 200 <= status < 300
    assert body["borrower"] == "丁"


def test_s16_the_librarian_can_still_fulfil_the_released_head(base_url, http):
    """#42/S16, DEC3/#42/TC7: 放行仍然生效时乙本人出现，馆员当场兑现给他。"""
    book, yi = _solo_locked_book(http, base_url)
    _defer(http, base_url, book["id"])
    _borrow(http, base_url, book["id"], "丁")
    _restock(http, base_url, book["id"], 1)
    _defer(http, base_url, book["id"])

    status, _, body = _fulfill(http, base_url, book["id"])

    assert 200 <= status < 300
    assert body["reservation"]["id"] == yi["id"]
    assert body["reservation"]["holder"] == "乙"
    assert body["reservation"]["status"] != "waiting"
    assert body["loan"]["borrower"] == "乙"
    assert body["loan"]["book_id"] == book["id"]
    assert set(body["loan"]) == LOAN_FIELDS
    assert _queue(http, base_url, book["id"]) == []


def test_the_release_never_lets_the_head_borrow_past_the_queue(base_url, http):
    """DEC6 保留 ④/#42/TC6: 放行让出的是「别人可以先拿」，不是「他自己不能拿」。

    乙本人的普通借阅在放行期间照样被拒 —— 他要拿到这一本仍然只能经由馆员兑现。
    """
    book, _ = _solo_locked_book(http, base_url)
    _defer(http, base_url, book["id"])

    status, _, body = _borrow(http, base_url, book["id"], "乙")

    assert 400 <= status < 500
    assert "id" not in body
    assert _stock(http, base_url, book["id"]) == 1


def test_a_never_deferred_single_waiter_still_locks_the_copy(base_url, http):
    """DEC6 保留 ①/#42/TC6: 从未被处置过的 SKU 上，队列非空一律拒绝原样成立。"""
    book = _book(http, base_url, "从未处置过", 0)
    _reserve(http, base_url, book["id"], "乙")
    _restock(http, base_url, book["id"], 1)

    status, _, body = _borrow(http, base_url, book["id"], "丁")

    assert 400 <= status < 500
    assert "id" not in body
    assert _stock(http, base_url, book["id"]) == 1


def test_deferring_with_other_waiters_produces_no_release(base_url, http):
    """DEC6 保留 ②/#42/TC6: 处置发生时队列还有其他等待者的，不产生放行。"""
    book = _book(http, base_url, "还有别的等待者", 0)
    _reserve(http, base_url, book["id"], "乙")
    _reserve(http, base_url, book["id"], "丙")
    _restock(http, base_url, book["id"], 1)

    _defer(http, base_url, book["id"])
    status, _, body = _borrow(http, base_url, book["id"], "丁")

    assert 400 <= status < 500
    assert "id" not in body
    assert _stock(http, base_url, book["id"]) == 1


def test_a_release_produced_with_other_waiters_absent_is_not_retroactive(base_url, http):
    """DEC6 条件/#42/TC6: 放行只由「处置当时队列只剩一人」产生，不会日后自动生效。

    队列还有其他等待者时处置一次，其余等待者被兑现走之后队列自然缩到一人 ——
    这不是一次单人处置，丁的普通借阅仍然被拒。
    """
    book = _book(http, base_url, "不追溯", 0)
    _reserve(http, base_url, book["id"], "乙")
    _reserve(http, base_url, book["id"], "丙")
    _restock(http, base_url, book["id"], 1)
    _defer(http, base_url, book["id"])
    _fulfill(http, base_url, book["id"])
    _restock(http, base_url, book["id"], 1)

    status, _, body = _borrow(http, base_url, book["id"], "丁")

    assert 400 <= status < 500
    assert "id" not in body
    assert _stock(http, base_url, book["id"]) == 1


def test_the_release_belongs_to_its_own_sku(base_url, http):
    """#42/TC3: 放行是属于该 SKU 的状态量，不影响别的 SKU 的队列规则。"""
    book, _ = _solo_locked_book(http, base_url)
    other = _book(http, base_url, "另一个 SKU", 0)
    _reserve(http, base_url, other["id"], "乙")
    _restock(http, base_url, other["id"], 1)

    _defer(http, base_url, book["id"])

    status, _, body = _borrow(http, base_url, other["id"], "丁")
    assert 400 <= status < 500
    assert "id" not in body
    assert _stock(http, base_url, other["id"]) == 1


def test_the_release_is_not_consumed_when_the_borrow_fails_on_stock(base_url, http):
    """#42/TC6/#42/TC9: 借阅未成功就不消耗放行 —— 消耗与扣减在同一事务内推进。"""
    book = _book(http, base_url, "放行但没有库存", 0)
    _reserve(http, base_url, book["id"], "乙")
    _defer(http, base_url, book["id"])

    exhausted, _, _ = _borrow(http, base_url, book["id"], "丁")
    _restock(http, base_url, book["id"], 1)
    status, _, body = _borrow(http, base_url, book["id"], "丁")

    assert exhausted == 409
    assert 200 <= status < 300
    assert body["borrower"] == "丁"
    assert _stock(http, base_url, book["id"]) == 0


def test_the_release_survives_across_instances(two_process_urls, http):
    """#42/TC9: 放行状态与库存、Loan 同库持久化，跨请求、跨实例可见。"""
    (first_url, second_url), server_errors = two_process_urls
    book, yi = _solo_locked_book(http, first_url)

    _defer(http, first_url, book["id"])
    status, _, loan = _borrow(http, second_url, book["id"], "丁")
    _restock(http, second_url, book["id"], 1)
    rejected, _, body = _borrow(http, first_url, book["id"], "丁")
    queue = _queue(http, second_url, book["id"])

    assert 200 <= status < 300
    assert loan["borrower"] == "丁"
    assert 400 <= rejected < 500
    assert "id" not in body
    assert [(item["id"], item["position"]) for item in queue] == [(yi["id"], 1)]

    server_errors.seek(0)
    assert server_errors.read() == ""
