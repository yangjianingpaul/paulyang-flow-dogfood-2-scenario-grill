"""图书借还记录 API —— 单文件、仅标准库 (TC2)。

数据只存在于进程内存，进程重启即清空 (TC19)。
"""

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000

# 进程内存储 (TC19)。id 计数器只增不减，保证 id 永不复用 (TC14)。
_books: dict[str, dict] = {}
_book_seq = 0
_loans: dict[str, dict] = {}
_loan_seq = 0


class Conflict(Exception):
    """借出被拒：目标 book 已存在一条未归还的 Loan (TC12)。"""


class NotFound(Exception):
    """引用了一个不存在的实体 (TC13)。"""


def reset_state() -> None:
    """清空进程内存储，回到干净的初始状态。"""
    global _book_seq, _loan_seq
    _books.clear()
    _book_seq = 0
    _loans.clear()
    _loan_seq = 0


def create_book(title: str) -> dict:
    """新建一本书，id 形态 bk_<n>，n 从 1 起单调递增 (TC9)。"""
    global _book_seq
    _book_seq += 1
    book = {"id": f"bk_{_book_seq}", "title": title}
    _books[book["id"]] = book
    return book


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
    if book_id not in _books:
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
        self._send_json(404, {"error": f"no route for GET {self.path}"})

    def _handle_create_book(self):
        """POST /books → 201 {"id", "title"} (TC3, TC8)."""
        payload = self._read_json()
        if payload is None:
            return
        title = payload.get("title")
        if not isinstance(title, str):
            self._send_json(400, {"error": "title must be a string"})
            return
        self._send_json(201, create_book(title))

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

    def _send_json(self, status: int, body: dict):
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


if __name__ == "__main__":
    server = make_server()
    print(f"listening on http://localhost:{server.server_address[1]}")  # TC1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
