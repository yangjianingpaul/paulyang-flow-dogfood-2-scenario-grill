"""Ticket #11 / #9/S1: create a SKU with initial shared inventory."""

import sqlite3
from pathlib import Path

import pytest

import app


def test_s1_create_sku_returns_its_initial_available_stock(base_url, http):
    """#9/S1, TC1-TC4: the response identifies the SKU and shows stock 500."""
    status, headers, body = http(
        "POST",
        f"{base_url}/books",
        {"title": "三体", "initial_stock": 500},
    )

    assert status == 201
    assert headers["Content-Type"] == "application/json"
    assert isinstance(body["id"], str) and body["id"]
    assert body == {
        "id": body["id"],
        "title": "三体",
        "available_stock": 500,
    }


@pytest.mark.parametrize("initial_stock", [None, True, 1.5, "500", -1])
def test_initial_stock_must_be_a_non_negative_integer(
    base_url, http, initial_stock
):
    """TC1, TC3: invalid stock is rejected instead of creating a SKU."""
    status, _, body = http(
        "POST",
        f"{base_url}/books",
        {"title": "三体", "initial_stock": initial_stock},
    )

    assert status == 400
    assert isinstance(body["error"], str) and body["error"]


def test_initial_stock_is_required(base_url, http):
    """TC1: initial_stock is part of the minimum POST /books request."""
    status, _, body = http("POST", f"{base_url}/books", {"title": "三体"})

    assert status == 400
    assert isinstance(body["error"], str) and body["error"]


def test_created_sku_is_written_to_the_worktree_inventory_database(base_url, http):
    """TC4, TC6: creation atomically persists stock in the shared SQLite file."""
    _, _, body = http(
        "POST",
        f"{base_url}/books",
        {"title": "三体", "initial_stock": 500},
    )

    expected = Path(app.__file__).resolve().parent / ".runtime" / "inventory.sqlite3"
    assert app.INVENTORY_DB_PATH == expected
    assert expected.is_file()
    with sqlite3.connect(expected) as connection:
        stored = connection.execute(
            "SELECT title, available_stock FROM books WHERE public_id = ?",
            (body["id"],),
        ).fetchone()
    assert stored == ("三体", 500)


def test_process_entry_accepts_a_port_argument():
    """TC1: the process entry accepts --port <integer>."""
    assert app.parse_args(["--port", "8123"]).port == 8123
