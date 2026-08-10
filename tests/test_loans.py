"""Regression tests for #1/S2 – #1/S5 —— 完整借还生命周期。"""

import re
from datetime import datetime

import pytest


ISO_UTC_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


@pytest.fixture
def book(base_url, http):
    """#1/S1 作为 setup：先建一本书拿 id (DEC3, TC13)。"""
    _, _, body = http("POST", f"{base_url}/books", {"title": "三体"})
    return body


def test_borrow_returns_201_with_unreturned_loan(base_url, http, book):
    """#1/S2: 请求成功，响应体含一个 loan id，该记录处于「未归还」(TC4, TC8, TC10)。"""
    status, headers, body = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )

    assert status == 201
    assert headers["Content-Type"] == "application/json"
    assert isinstance(body["id"], str) and body["id"]
    assert body["book_id"] == book["id"]
    assert body["borrower"] == "paul"
    assert body["returned_at"] is None
    assert set(body) == {"id", "book_id", "borrower", "returned_at"}


def test_loan_id_shape_is_ln_n_on_its_own_counter(base_url, http, book):
    """TC10: id 形态 "ln_<n>"，独立于 book 的计数器。"""
    _, _, first = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )
    _, _, other_book = http("POST", f"{base_url}/books", {"title": "球状闪电"})
    _, _, second = http(
        "POST", f"{base_url}/loans", {"book_id": other_book["id"], "borrower": "alice"}
    )

    assert re.fullmatch(r"ln_\d+", first["id"])
    assert first["id"] == "ln_1"
    assert second["id"] == "ln_2"


def test_borrower_is_a_free_string_kept_verbatim(base_url, http, book):
    """TC11: borrower 为自由字符串，不作校验、不作规范化。"""
    _, _, body = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "  Alice O'Hara  "}
    )

    assert body["borrower"] == "  Alice O'Hara  "


def test_second_borrow_of_same_book_is_rejected_with_readable_reason(base_url, http, book):
    """#1/S3: 请求失败，响应能看出失败原因是「这本书已被借出」(TC7, TC12, TC16)。"""
    http("POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"})

    status, _, body = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "alice"}
    )

    assert status == 409
    assert isinstance(body["error"], str) and body["error"]
    assert "loan" in body["error"].lower()


def test_borrowing_an_unknown_book_is_rejected(base_url, http):
    """TC13: Loan 的 book_id 必须指向已存在的 Book；TC7: 不存在用 404。"""
    status, _, body = http(
        "POST", f"{base_url}/loans", {"book_id": "bk_404", "borrower": "paul"}
    )

    assert status == 404
    assert isinstance(body["error"], str) and body["error"]


def test_return_marks_loan_returned_with_non_empty_timestamp(base_url, http, book):
    """#1/S4: 请求成功，响应体显示该 loan 已归还且归还时间非空 (TC5, TC8, TC15, TC20)。"""
    _, _, loan = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )

    status, _, body = http("POST", f"{base_url}/loans/{loan['id']}/return")

    assert status == 200
    assert body["id"] == loan["id"]
    assert body["book_id"] == book["id"]
    assert body["borrower"] == "paul"
    assert isinstance(body["returned_at"], str) and body["returned_at"]
    assert set(body) == {"id", "book_id", "borrower", "returned_at"}


def test_returned_at_is_utc_iso8601_ending_in_z(base_url, http, book):
    """TC20: returned_at 为 UTC ISO-8601 且以 Z 结尾，由服务端在处理时刻生成。"""
    _, _, loan = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )

    _, _, body = http("POST", f"{base_url}/loans/{loan['id']}/return")

    assert ISO_UTC_Z.fullmatch(body["returned_at"])
    # 可被解析回一个真实时刻
    datetime.fromisoformat(body["returned_at"].replace("Z", "+00:00"))


def test_returning_an_unknown_loan_is_404(base_url, http):
    """TC5 的路由骨架：未知 loan id 返回 404 而非崩溃。"""
    status, _, body = http("POST", f"{base_url}/loans/ln_404/return")

    assert status == 404
    assert isinstance(body["error"], str) and body["error"]


def test_borrow_again_after_return_succeeds_with_a_new_loan_id(base_url, http):
    """#1/S5: 归还后再借成功，返回新的 loan id 且不等于 <LOAN_1> (TC14, TC16)。"""
    _, _, book = http("POST", f"{base_url}/books", {"title": "三体"})
    _, _, loan_1 = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )
    http("POST", f"{base_url}/loans/{loan_1['id']}/return")

    status, _, loan_2 = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "alice"}
    )

    assert status == 201
    assert loan_2["id"] != loan_1["id"]
    assert loan_2["borrower"] == "alice"
    assert loan_2["returned_at"] is None


def test_returned_loan_is_kept_and_its_id_never_rewritten(base_url, http):
    """TC14: 已归还的 Loan 记录不被删除，其 id 不被改写。"""
    import app

    _, _, book = http("POST", f"{base_url}/books", {"title": "三体"})
    _, _, loan_1 = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )
    http("POST", f"{base_url}/loans/{loan_1['id']}/return")
    _, _, loan_2 = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "alice"}
    )

    stored = app.list_loans()
    assert [loan["id"] for loan in stored] == [loan_1["id"], loan_2["id"]]
    assert stored[0]["returned_at"] is not None


def test_at_most_one_unreturned_loan_per_book_across_a_full_cycle(base_url, http):
    """TC12: 对任一 book_id，任何时刻至多存在一条 returned_at 为 null 的 Loan。"""
    import app

    _, _, book = http("POST", f"{base_url}/books", {"title": "三体"})

    def open_loans():
        return [
            loan
            for loan in app.list_loans()
            if loan["book_id"] == book["id"] and loan["returned_at"] is None
        ]

    assert open_loans() == []

    _, _, loan_1 = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )
    assert len(open_loans()) == 1

    http("POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "alice"})
    assert len(open_loans()) == 1

    http("POST", f"{base_url}/loans/{loan_1['id']}/return")
    assert open_loans() == []

    http("POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "alice"})
    assert len(open_loans()) == 1


def test_other_books_are_unaffected_by_a_conflicting_book(base_url, http):
    """TC12 的边界：冲突只作用于同一 book_id。"""
    _, _, book_a = http("POST", f"{base_url}/books", {"title": "三体"})
    _, _, book_b = http("POST", f"{base_url}/books", {"title": "球状闪电"})
    http("POST", f"{base_url}/loans", {"book_id": book_a["id"], "borrower": "paul"})

    status, _, body = http(
        "POST", f"{base_url}/loans", {"book_id": book_b["id"], "borrower": "alice"}
    )

    assert status == 201
    assert body["book_id"] == book_b["id"]


def test_borrow_requires_book_id_and_borrower(base_url, http, book):
    """TC4 的请求形状：缺字段或类型不对时以可读错误拒绝，而非崩溃。"""
    for payload in ({"borrower": "paul"}, {"book_id": book["id"]}, {}):
        status, _, body = http("POST", f"{base_url}/loans", payload)

        assert status == 400
        assert isinstance(body["error"], str) and body["error"]
