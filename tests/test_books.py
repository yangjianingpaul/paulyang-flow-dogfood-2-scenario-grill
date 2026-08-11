"""Regression tests for #1/S1 —— 建一本书《三体》。"""

import re


def test_create_book_returns_201_with_id_and_title(base_url, http):
    """#1/S1: 请求成功，响应体含一个 book id (TC3, TC8)."""
    status, headers, body = http(
        "POST", f"{base_url}/books", {"title": "三体", "initial_stock": 1}
    )

    assert status == 201
    assert headers["Content-Type"] == "application/json"
    assert body["title"] == "三体"
    assert isinstance(body["id"], str) and body["id"]
    assert body["available_stock"] == 1
    assert set(body) == {"id", "title", "available_stock"}


def test_book_id_shape_is_bk_n_starting_at_1(base_url, http):
    """TC9: id 形态 "bk_<n>"，n 为从 1 起的进程内单调递增整数。"""
    _, _, first = http(
        "POST", f"{base_url}/books", {"title": "三体", "initial_stock": 1}
    )
    _, _, second = http(
        "POST", f"{base_url}/books", {"title": "球状闪电", "initial_stock": 1}
    )

    assert re.fullmatch(r"bk_\d+", first["id"])
    assert first["id"] == "bk_1"
    assert second["id"] == "bk_2"


def test_book_ids_are_unique_and_never_reused(base_url, http):
    """TC14: Book id 全局唯一且永不复用。"""
    ids = [
        http(
            "POST",
            f"{base_url}/books",
            {"title": f"book-{i}", "initial_stock": 1},
        )[2]["id"]
        for i in range(5)
    ]

    assert len(set(ids)) == 5


def test_created_book_records_no_timestamp(base_url, http):
    """TC21: Book 不记录创建时间。"""
    _, _, body = http(
        "POST", f"{base_url}/books", {"title": "三体", "initial_stock": 1}
    )

    assert "created_at" not in body


def test_unknown_route_is_404(base_url, http):
    """路由骨架：未实现的路径返回 404 而非崩溃。"""
    status, _, body = http("GET", f"{base_url}/nope")

    assert status == 404
    assert isinstance(body["error"], str) and body["error"]
