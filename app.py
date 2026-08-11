"""图书借还记录 API —— 单文件、仅标准库。

SKU 与库存存放在 worktree 内的 SQLite；既有 Loan 生命周期仍由进程内状态维护。
"""

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PORT = 8000
INVENTORY_DB_PATH = Path(__file__).resolve().parent / ".runtime" / "inventory.sqlite3"

# 既有 Loan 生命周期仍使用进程内存储；本轮不改写它的字段和归还行为。
_loans: dict[str, dict] = {}
_loan_seq = 0


class Conflict(Exception):
    """借出被拒：目标 book 已存在一条未归还的 Loan (TC12)。"""


class NotFound(Exception):
    """引用了一个不存在的实体 (TC13)。"""


def reset_state() -> None:
    """测试辅助：在服务启动前清空 SQLite 与进程内 Loan 状态。"""
    global _loan_seq
    for suffix in ("", "-wal", "-shm"):
        Path(f"{INVENTORY_DB_PATH}{suffix}").unlink(missing_ok=True)
    _loans.clear()
    _loan_seq = 0


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


def _book_exists(book_id: str) -> bool:
    with closing(_connect_inventory()) as connection:
        row = connection.execute(
            "SELECT 1 FROM books WHERE public_id = ?",
            (book_id,),
        ).fetchone()
    return row is not None


def list_loans() -> list[dict]:
    """全部 Loan，含已归还的；已归还记录不被删除 (TC14)。"""
    return [dict(loan) for loan in _loans.values()]


def _open_loan_for(book_id: str) -> dict | None:
    """该 book 当前未归还的 Loan；不变量保证至多一条 (TC12)。"""
    for loan in _loans.values():
        if loan["book_id"] == book_id and loan["returned_at"] is None:
            return loan
    return None


def create_loan(book_id: str, borrower: str) -> dict:
    """借出一本书。

    book 必须存在 (TC13)，且当前没有未归还的 Loan (TC12)；
    borrower 是自由字符串，原样保存 (TC11)。
    """
    global _loan_seq
    if not _book_exists(book_id):
        raise NotFound(f"no book with id {book_id!r}")
    if _open_loan_for(book_id) is not None:
        raise Conflict("book already on loan")
    _loan_seq += 1
    loan = {
        "id": f"ln_{_loan_seq}",
        "book_id": book_id,
        "borrower": borrower,
        "returned_at": None,
    }
    _loans[loan["id"]] = loan
    return dict(loan)


def return_loan(loan_id: str) -> dict:
    """未归还 → 已归还的唯一入口，写入服务端当前时刻 (TC15, TC20)。"""
    loan = _loans.get(loan_id)
    if loan is None:
        raise NotFound(f"no loan with id {loan_id!r}")
    loan["returned_at"] = _now_iso8601_utc()
    return dict(loan)


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
        elif (loan_id := self._match_return_path(self.path)) is not None:
            self._handle_return_loan(loan_id)
        else:
            self._send_json(404, {"error": f"no route for POST {self.path}"})

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
        else:
            self._send_json(404, {"error": f"no route for GET {self.path}"})

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
        """POST /loans → 201 Loan / 404 未知 book / 409 已被借出 (TC4, TC7, TC8)."""
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
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


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
