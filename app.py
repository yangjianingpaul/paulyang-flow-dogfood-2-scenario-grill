"""图书借还记录 API —— 单文件、仅标准库。

SKU、库存与 Loan 存放在 worktree 内的共享 SQLite。
"""

import argparse
import json
import socket
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PORT = 8000
INVENTORY_DB_PATH = Path(__file__).resolve().parent / ".runtime" / "inventory.sqlite3"


class Conflict(Exception):
    """借出被拒：目标 SKU 的可用库存已经耗尽。"""


class NotFound(Exception):
    """引用了一个不存在的实体 (TC13)。"""


class ConcurrentHTTPServer(ThreadingHTTPServer):
    """Thread-per-request server with room for the scenario's connection burst."""

    request_queue_size = socket.SOMAXCONN


def reset_state() -> None:
    """测试辅助：在服务启动前清空共享 SQLite 状态。"""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{INVENTORY_DB_PATH}{suffix}").unlink(missing_ok=True)


def _connect_inventory() -> sqlite3.Connection:
    """打开所有服务实例共用的库存数据库，并确保 schema 存在。"""
    INVENTORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(INVENTORY_DB_PATH, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS books (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT UNIQUE,
            title TEXT NOT NULL,
            available_stock INTEGER NOT NULL CHECK (available_stock >= 0)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS loans (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT UNIQUE,
            book_id TEXT NOT NULL,
            borrower TEXT NOT NULL,
            returned_at TEXT,
            FOREIGN KEY (book_id) REFERENCES books(public_id)
        )
        """
    )
    return connection


def create_book(title: str, initial_stock: int) -> dict:
    """原子创建 SKU，并把初始库存写入共享 SQLite。"""
    with closing(_connect_inventory()) as connection:
        with connection:
            cursor = connection.execute(
                "INSERT INTO books (title, available_stock) VALUES (?, ?)",
                (title, initial_stock),
            )
            public_id = f"bk_{cursor.lastrowid}"
            connection.execute(
                "UPDATE books SET public_id = ? WHERE sequence = ?",
                (public_id, cursor.lastrowid),
            )
    return {
        "id": public_id,
        "title": title,
        "available_stock": initial_stock,
    }


def get_book(book_id: str) -> dict:
    """从共享状态源读取单个 SKU 的当前库存。"""
    with closing(_connect_inventory()) as connection:
        row = connection.execute(
            """
            SELECT public_id, title, available_stock
            FROM books
            WHERE public_id = ?
            """,
            (book_id,),
        ).fetchone()
    if row is None:
        raise NotFound(f"no book with id {book_id!r}")
    public_id, title, available_stock = row
    return {
        "id": public_id,
        "title": title,
        "available_stock": available_stock,
    }


def restock_book(book_id: str, quantity: int) -> dict:
    """原子增加共享库存，并返回本次更新后的 SKU。"""
    with closing(_connect_inventory()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE books
                SET available_stock = available_stock + ?
                WHERE public_id = ?
                """,
                (quantity, book_id),
            )
            if updated.rowcount == 0:
                raise NotFound(f"no book with id {book_id!r}")
            public_id, title, available_stock = connection.execute(
                """
                SELECT public_id, title, available_stock
                FROM books
                WHERE public_id = ?
                """,
                (book_id,),
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "id": public_id,
        "title": title,
        "available_stock": available_stock,
    }


def list_loans() -> list[dict]:
    """全部 Loan，含已归还的；已归还记录不被删除 (TC14)。"""
    with closing(_connect_inventory()) as connection:
        rows = connection.execute(
            """
            SELECT public_id, book_id, borrower, returned_at
            FROM loans
            ORDER BY sequence
            """
        ).fetchall()
    return [
        {
            "id": public_id,
            "book_id": book_id,
            "borrower": borrower,
            "returned_at": returned_at,
        }
        for public_id, book_id, borrower, returned_at in rows
    ]


def create_loan(book_id: str, borrower: str) -> dict:
    """原子扣减共享库存并创建一条 Loan。"""
    with closing(_connect_inventory()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE books
                SET available_stock = available_stock - 1
                WHERE public_id = ? AND available_stock > 0
                """,
                (book_id,),
            )
            if updated.rowcount == 0:
                exists = connection.execute(
                    "SELECT 1 FROM books WHERE public_id = ?",
                    (book_id,),
                ).fetchone()
                if exists is None:
                    raise NotFound(f"no book with id {book_id!r}")
                raise Conflict("stock exhausted: book already on loan")

            cursor = connection.execute(
                "INSERT INTO loans (book_id, borrower) VALUES (?, ?)",
                (book_id, borrower),
            )
            public_id = f"ln_{cursor.lastrowid}"
            connection.execute(
                "UPDATE loans SET public_id = ? WHERE sequence = ?",
                (public_id, cursor.lastrowid),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "id": public_id,
        "book_id": book_id,
        "borrower": borrower,
        "returned_at": None,
    }


def return_loan(loan_id: str) -> dict:
    """未归还 → 已归还的唯一入口，写入服务端当前时刻 (TC15, TC20)。"""
    returned_at = _now_iso8601_utc()
    with closing(_connect_inventory()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT book_id, borrower, returned_at
                FROM loans
                WHERE public_id = ?
                """,
                (loan_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"no loan with id {loan_id!r}")
            book_id, borrower, existing_returned_at = row
            if existing_returned_at is None:
                connection.execute(
                    "UPDATE loans SET returned_at = ? WHERE public_id = ?",
                    (returned_at, loan_id),
                )
                connection.execute(
                    """
                    UPDATE books
                    SET available_stock = available_stock + 1
                    WHERE public_id = ?
                    """,
                    (book_id,),
                )
            else:
                returned_at = existing_returned_at
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "id": loan_id,
        "book_id": book_id,
        "borrower": borrower,
        "returned_at": returned_at,
    }


def _now_iso8601_utc() -> str:
    """UTC ISO-8601，以 Z 结尾 (TC20)。"""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        if self.path == "/books":
            self._handle_create_book()
        elif self.path == "/loans":
            self._handle_create_loan()
        elif (book_id := self._match_restock_path(self.path)) is not None:
            self._handle_restock_book(book_id)
        elif (loan_id := self._match_return_path(self.path)) is not None:
            self._handle_return_loan(loan_id)
        else:
            self._send_json(404, {"error": f"no route for POST {self.path}"})

    @staticmethod
    def _match_restock_path(path: str) -> str | None:
        """POST /books/<book_id>/restock → book_id。"""
        parts = path.split("/")
        if len(parts) == 4 and parts[:2] == ["", "books"] and parts[3] == "restock":
            return parts[2] or None
        return None

    @staticmethod
    def _match_return_path(path: str) -> str | None:
        """POST /loans/<loan_id>/return → loan_id (TC5)."""
        parts = path.split("/")
        if len(parts) == 4 and parts[:2] == ["", "loans"] and parts[3] == "return":
            return parts[2] or None
        return None

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        # 该端点不定义查询参数，查询串一律忽略 (TC17)。
        path = urlsplit(self.path).path
        if path == "/loans":
            self._handle_list_loans()
        elif (book_id := self._match_book_path(path)) is not None:
            self._handle_get_book(book_id)
        else:
            self._send_json(404, {"error": f"no route for GET {self.path}"})

    @staticmethod
    def _match_book_path(path: str) -> str | None:
        """GET /books/<book_id> → book_id。"""
        parts = path.split("/")
        if len(parts) == 3 and parts[:2] == ["", "books"]:
            return parts[2] or None
        return None

    def _handle_get_book(self, book_id: str):
        """GET /books/<id> → 200 Book，直接返回共享库存。"""
        try:
            book = get_book(book_id)
        except NotFound as exc:
            self._send_json(404, {"error": str(exc)})
        else:
            self._send_json(200, book)

    def _handle_list_loans(self):
        """GET /loans → 200 Loan 数组，含已归还的，顺序不作规定 (TC6, TC8, TC17, TC18)。"""
        self._send_json(200, list_loans())

    def _handle_create_book(self):
        """POST /books → 201 Book，含原子写入的初始可用库存。"""
        payload = self._read_json()
        if payload is None:
            return
        title = payload.get("title")
        initial_stock = payload.get("initial_stock")
        if not isinstance(title, str):
            self._send_json(400, {"error": "title must be a string"})
            return
        if (
            not isinstance(initial_stock, int)
            or isinstance(initial_stock, bool)
            or initial_stock < 0
        ):
            self._send_json(
                400,
                {"error": "initial_stock must be a non-negative integer"},
            )
            return
        self._send_json(201, create_book(title, initial_stock))

    def _handle_create_loan(self):
        """POST /loans → 201 Loan / 404 未知 book / 409 库存耗尽。"""
        payload = self._read_json()
        if payload is None:
            return
        book_id = payload.get("book_id")
        borrower = payload.get("borrower")
        if not isinstance(book_id, str) or not book_id:
            self._send_json(400, {"error": "book_id must be a non-empty string"})
            return
        if not isinstance(borrower, str) or not borrower:
            self._send_json(400, {"error": "borrower must be a non-empty string"})
            return
        try:
            loan = create_loan(book_id, borrower)
        except NotFound as exc:
            self._send_json(404, {"error": str(exc)})
        except Conflict as exc:
            self._send_json(409, {"error": str(exc)})
        else:
            self._send_json(201, loan)

    def _handle_restock_book(self, book_id: str):
        """POST /books/<id>/restock → 200 更新后的 Book。"""
        payload = self._read_json()
        if payload is None:
            return
        quantity = payload.get("quantity")
        if (
            not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or quantity <= 0
        ):
            self._send_json(
                400,
                {"error": "quantity must be a positive integer"},
            )
            return
        try:
            book = restock_book(book_id, quantity)
        except NotFound as exc:
            self._send_json(404, {"error": str(exc)})
        else:
            self._send_json(200, book)

    def _handle_return_loan(self, loan_id: str):
        """POST /loans/<loan_id>/return → 200 Loan (TC5, TC8)；无请求体。"""
        try:
            loan = return_loan(loan_id)
        except NotFound as exc:
            self._send_json(404, {"error": str(exc)})
        else:
            self._send_json(200, loan)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "request body must be valid JSON"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "request body must be a JSON object"})
            return None
        return payload

    def _send_json(self, status: int, body: dict | list):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):  # 保持终端输出干净
        pass


def make_server(port: int = PORT) -> ThreadingHTTPServer:
    return ConcurrentHTTPServer(("127.0.0.1", port), Handler)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the book inventory API")
    parser.add_argument("--port", type=int, default=PORT)
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    server = make_server(args.port)
    print(f"listening on http://localhost:{server.server_address[1]}")  # TC1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
