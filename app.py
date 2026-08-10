"""图书借还记录 API —— 单文件、仅标准库 (TC2)。

数据只存在于进程内存，进程重启即清空 (TC19)。
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000

# 进程内存储 (TC19)。id 计数器只增不减，保证 id 永不复用 (TC14)。
_books: dict[str, dict] = {}
_book_seq = 0


def reset_state() -> None:
    """清空进程内存储，回到干净的初始状态。"""
    global _book_seq
    _books.clear()
    _book_seq = 0


def create_book(title: str) -> dict:
    """新建一本书，id 形态 bk_<n>，n 从 1 起单调递增 (TC9)。"""
    global _book_seq
    _book_seq += 1
    book = {"id": f"bk_{_book_seq}", "title": title}
    _books[book["id"]] = book
    return book


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        if self.path == "/books":
            self._handle_create_book()
        else:
            self._send_json(404, {"error": f"no route for POST {self.path}"})

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
