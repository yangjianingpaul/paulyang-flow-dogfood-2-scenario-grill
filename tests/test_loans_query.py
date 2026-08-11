"""Regression tests for #1/S6 —— 全量借还流水查询。"""

import pytest


@pytest.fixture
def book(base_url, http):
    """#1/S1 作为 setup：先建一本书拿 id (DEC3, TC13)。"""
    _, _, body = http(
        "POST", f"{base_url}/books", {"title": "三体", "initial_stock": 1}
    )
    return body


def test_list_loans_after_full_scenario_prefix(base_url, http, book):
    """#1/S6: 恰好 2 条记录，paul 已归还 / alice 未归还 (TC6, TC17, DEC5)。

    顺序不构成判据 (TC18)，故断言与顺序无关。
    """
    # #1/S2 – #1/S5 作为 setup
    _, _, loan_1 = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )
    http("POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "alice"})
    http("POST", f"{base_url}/loans/{loan_1['id']}/return")
    _, _, loan_2 = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "alice"}
    )

    status, headers, body = http("GET", f"{base_url}/loans")

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert isinstance(body, list)
    assert len(body) == 2

    by_borrower = {loan["borrower"]: loan for loan in body}
    assert set(by_borrower) == {"paul", "alice"}
    assert by_borrower["paul"]["id"] == loan_1["id"]
    assert by_borrower["paul"]["returned_at"] is not None
    assert by_borrower["alice"]["id"] == loan_2["id"]
    assert by_borrower["alice"]["returned_at"] is None


def test_list_loans_elements_have_the_loan_shape(base_url, http, book):
    """TC6: 元素形状同 TC4/TC10。"""
    _, _, loan = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )

    _, _, body = http("GET", f"{base_url}/loans")

    assert body == [loan]
    assert set(body[0]) == {"id", "book_id", "borrower", "returned_at"}


def test_list_loans_is_empty_array_before_any_loan(base_url, http, book):
    """TC6: 响应始终是 JSON 数组；没有 Loan 时为空数组而非 404。"""
    status, _, body = http("GET", f"{base_url}/loans")

    assert status == 200
    assert body == []


def test_list_loans_is_not_filtered_by_returned_state(base_url, http, book):
    """DEC5, TC17: 返回全部流水，含已归还的，不按归还状态筛选。"""
    _, _, loan = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )
    http("POST", f"{base_url}/loans/{loan['id']}/return")

    _, _, body = http("GET", f"{base_url}/loans")

    assert [entry["id"] for entry in body] == [loan["id"]]
    assert body[0]["returned_at"] is not None


def test_list_loans_spans_all_books(base_url, http):
    """TC17: 不以书为入口，全部 Loan 在一次响应中返回。"""
    _, _, book_a = http(
        "POST", f"{base_url}/books", {"title": "三体", "initial_stock": 1}
    )
    _, _, book_b = http(
        "POST", f"{base_url}/books", {"title": "球状闪电", "initial_stock": 1}
    )
    _, _, loan_a = http(
        "POST", f"{base_url}/loans", {"book_id": book_a["id"], "borrower": "paul"}
    )
    _, _, loan_b = http(
        "POST", f"{base_url}/loans", {"book_id": book_b["id"], "borrower": "alice"}
    )

    _, _, body = http("GET", f"{base_url}/loans")

    assert {entry["id"] for entry in body} == {loan_a["id"], loan_b["id"]}


def test_list_loans_ignores_query_string(base_url, http, book):
    """TC17: 该端点不定义任何查询参数，带查询串也不筛选。"""
    _, _, loan = http(
        "POST", f"{base_url}/loans", {"book_id": book["id"], "borrower": "paul"}
    )
    http("POST", f"{base_url}/loans/{loan['id']}/return")

    status, _, body = http("GET", f"{base_url}/loans?returned=false")

    assert status == 200
    assert [entry["id"] for entry in body] == [loan["id"]]


def test_unknown_get_route_is_still_404(base_url, http):
    """GET 路由表只认 /loans；其余仍以可读错误拒绝。"""
    status, _, body = http("GET", f"{base_url}/books")

    assert status == 404
    assert isinstance(body["error"], str) and body["error"]
