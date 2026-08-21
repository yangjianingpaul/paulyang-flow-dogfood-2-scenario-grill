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

# 等待集合中的每一条预约都处于该状态；兑现即移出集合 (TC2, TC6)。
WAITING = "waiting"

# 兑现响应中被移出等待集合的那条预约的状态；不落库，也不是新的中间状态 (DEC14)。
FULFILLED = "fulfilled"

# 普通借阅因等待队列被拒时的机器可读原因常量 (TC1)。
RESERVATION_QUEUE_ACTIVE = "reservation_queue_active"

# 该拒绝携带的下一步取值域；由请求者是否已在该 SKU 的等待队列中决定 (TC1, TC5)。
CREATE_RESERVATION = "create_reservation"
WAIT_FOR_FULFILLMENT = "wait_for_fulfillment"


class Conflict(Exception):
    """借出被拒：目标 SKU 的可用库存已经耗尽。"""


class ReservationRejected(Exception):
    """登记被拒：该 SKU 仍有可用库存，应当直接借阅 (TC4)。"""


class NoWaitingReservation(Exception):
    """兑现被拒：该 SKU 的等待队列为空，先于库存判断 (DEC5)。"""


class QueuedForReservation(Exception):
    """普通借阅被拒：该 SKU 有等待队列，只能由馆员兑现队首 (DEC11)。

    `next_action` 是调用方无需解析文案就能执行的下一步 (TC1, TC5)：
    请求者尚未在该 SKU 的等待队列中取 CREATE_RESERVATION，已在其中取
    WAIT_FOR_FULFILLMENT；队首与非队首不作区分。
    """

    def __init__(self, message: str, next_action: str):
        super().__init__(message)
        self.next_action = next_action


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
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reservations (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT UNIQUE,
            book_id TEXT NOT NULL,
            holder TEXT NOT NULL,
            -- 该 SKU 内的队列序，从 1 起连续；登记时追加至队尾，处置时被改写，
            -- 因此它与单调自增的 sequence 分开 (#42/TC5)。
            queue_order INTEGER NOT NULL,
            UNIQUE (book_id, holder),
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
    """原子扣减共享库存并创建一条 Loan。

    该 SKU 有等待队列时，任何普通借阅都不得消耗库存，队首本人也不例外 (DEC11, TC3)。
    队列判断先于库存判断：被拒的原因是队列存在，与当前是否有库存无关。
    """
    with closing(_connect_inventory()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM reservations WHERE book_id = ? LIMIT 1",
                (book_id,),
            ).fetchone():
                already_waiting = connection.execute(
                    """
                    SELECT 1
                    FROM reservations
                    WHERE book_id = ? AND holder = ?
                    LIMIT 1
                    """,
                    (book_id, borrower),
                ).fetchone()
                raise QueuedForReservation(
                    "reservation queue is not empty: this book must be handed "
                    "out by fulfilling the head of the waiting queue",
                    WAIT_FOR_FULFILLMENT if already_waiting else CREATE_RESERVATION,
                )

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


def create_reservation(book_id: str, holder: str) -> tuple[dict, bool]:
    """登记一条等待预约，返回 (Reservation, 是否新建)。

    重复登记复用既有等待预约。新 holder 的准入取决于该 SKU 的等待队列是否非空
    (#36/TC4, #36/DEC6)：队列非空时无论库存多少一律接受并排到队尾；队列为空时
    沿用既有准入，只有库存为零才允许进入等待。
    """
    with closing(_connect_inventory()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            book = connection.execute(
                "SELECT available_stock FROM books WHERE public_id = ?",
                (book_id,),
            ).fetchone()
            if book is None:
                raise NotFound(f"no book with id {book_id!r}")
            (available_stock,) = book

            existing = connection.execute(
                """
                SELECT sequence, public_id
                FROM reservations
                WHERE book_id = ? AND holder = ?
                """,
                (book_id, holder),
            ).fetchone()

            if existing is not None:
                sequence, public_id = existing
                created = False
            else:
                # 准入由该 SKU 的等待队列是否非空决定 (#36/TC4, #36/DEC6)：队列非空时
                # 队列优先于可用库存，新 holder 一律追加至队尾；队列为空时 #26/DEC2
                # 原样成立 —— 有库存则拒绝并提示直接借阅，无库存则登记成功。
                queue_active = connection.execute(
                    "SELECT 1 FROM reservations WHERE book_id = ? LIMIT 1",
                    (book_id,),
                ).fetchone()
                if queue_active is None and available_stock > 0:
                    raise ReservationRejected(
                        "stock available: borrow this book directly "
                        "instead of reserving it"
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO reservations (book_id, holder, queue_order)
                    VALUES (
                        ?,
                        ?,
                        (
                            SELECT COALESCE(MAX(queue_order), 0) + 1
                            FROM reservations
                            WHERE book_id = ?
                        )
                    )
                    """,
                    (book_id, holder, book_id),
                )
                sequence = cursor.lastrowid
                public_id = f"rs_{sequence}"
                connection.execute(
                    "UPDATE reservations SET public_id = ? WHERE sequence = ?",
                    (public_id, sequence),
                )
                created = True

            (position,) = connection.execute(
                """
                SELECT COUNT(*)
                FROM reservations
                WHERE book_id = ? AND queue_order <= (
                    SELECT queue_order FROM reservations WHERE sequence = ?
                )
                """,
                (book_id, sequence),
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return (
        {
            "id": public_id,
            "book_id": book_id,
            "holder": holder,
            "status": WAITING,
            "position": position,
        },
        created,
    )


def list_reservations(book_id: str) -> list[dict]:
    """该 SKU 的等待预约，按当前队列顺序排列 (TC5, #42/TC8)。"""
    with closing(_connect_inventory()) as connection:
        book = connection.execute(
            "SELECT 1 FROM books WHERE public_id = ?",
            (book_id,),
        ).fetchone()
        if book is None:
            raise NotFound(f"no book with id {book_id!r}")
        rows = connection.execute(
            """
            SELECT public_id, holder
            FROM reservations
            WHERE book_id = ?
            ORDER BY queue_order
            """,
            (book_id,),
        ).fetchall()
    return [
        {
            "id": public_id,
            "book_id": book_id,
            "holder": holder,
            "status": WAITING,
            "position": position,
        }
        for position, (public_id, holder) in enumerate(rows, start=1)
    ]


def fulfill_head_reservation(book_id: str) -> dict:
    """兑现当前队首：一个原子事务内扣库存、移出等待集合并创建 Loan (TC3, TC4)。

    先判断等待者、再判断库存：空队列一律失败为「没有等待者」，与库存无关 (DEC5)。
    任一失败分支都在 rollback 后返回，Book、Reservation 与 Loan 均不改变 (TC4)。
    """
    with closing(_connect_inventory()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            book = connection.execute(
                "SELECT available_stock FROM books WHERE public_id = ?",
                (book_id,),
            ).fetchone()
            if book is None:
                raise NotFound(f"no book with id {book_id!r}")
            (available_stock,) = book

            head = connection.execute(
                """
                SELECT sequence, public_id, holder
                FROM reservations
                WHERE book_id = ?
                ORDER BY queue_order
                LIMIT 1
                """,
                (book_id,),
            ).fetchone()
            if head is None:
                raise NoWaitingReservation(
                    "no waiting reservation: nobody is queued for this book"
                )
            sequence, reservation_id, holder = head

            if available_stock <= 0:
                raise Conflict(
                    "stock exhausted: no available stock to fulfil this "
                    "reservation until a copy is returned"
                )

            connection.execute(
                """
                UPDATE books
                SET available_stock = available_stock - 1
                WHERE public_id = ? AND available_stock > 0
                """,
                (book_id,),
            )
            connection.execute(
                "DELETE FROM reservations WHERE sequence = ?",
                (sequence,),
            )
            cursor = connection.execute(
                "INSERT INTO loans (book_id, borrower) VALUES (?, ?)",
                (book_id, holder),
            )
            loan_id = f"ln_{cursor.lastrowid}"
            connection.execute(
                "UPDATE loans SET public_id = ? WHERE sequence = ?",
                (loan_id, cursor.lastrowid),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "reservation": {
            "id": reservation_id,
            "book_id": book_id,
            "holder": holder,
            "status": FULFILLED,
            # 兑现只发生在队首，这里记录它被兑现时所处的位置 (DEC3, DEC10)。
            "position": 1,
        },
        "loan": {
            "id": loan_id,
            "book_id": book_id,
            # 兑现出的 Loan 的 borrower 与 holder 逐字相同 (DEC7)。
            "borrower": holder,
            "returned_at": None,
        },
    }


def defer_head_reservation(book_id: str) -> dict:
    """处置长期不出现的队首：把他排到当前队列的末位 (#42/TC1, #42/TC5)。

    只改队列序 —— 不新建、不删除任何等待预约，被处置者的 id 与 holder 原样保留，
    他仍然处于等待中，其余等待者按原相对顺序整体前移一位 (DEC1, DEC2, #42/TC3)。
    同一事务内不触碰 available_stock，也不创建 Loan：这一步不发书，把书交给新队首
    仍然是馆员后续一次独立的兑现动作 (DEC4, #42/TC4)。
    """
    with closing(_connect_inventory()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            book = connection.execute(
                "SELECT 1 FROM books WHERE public_id = ?",
                (book_id,),
            ).fetchone()
            if book is None:
                raise NotFound(f"no book with id {book_id!r}")

            queue = connection.execute(
                """
                SELECT sequence, public_id, holder
                FROM reservations
                WHERE book_id = ?
                ORDER BY queue_order
                """,
                (book_id,),
            ).fetchall()
            if not queue:
                raise NoWaitingReservation(
                    "no waiting reservation: nobody is queued for this book"
                )

            # 队首移到末位，其余整体前移一位；队列只剩他一人时顺序不变 (#42/TC5)。
            head, *rest = queue
            for position, (sequence, _, _) in enumerate([*rest, head], start=1):
                connection.execute(
                    "UPDATE reservations SET queue_order = ? WHERE sequence = ?",
                    (position, sequence),
                )
            _, reservation_id, holder = head
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "id": reservation_id,
        "book_id": book_id,
        "holder": holder,
        # 处置只调整队列，被处置者仍然处于等待中 (DEC1, #42/TC3)。
        "status": WAITING,
        # 处置完成后的位置：当前队列的末位 (DEC2, #42/TC1)。
        "position": len(queue),
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
        elif self.path == "/reservations":
            self._handle_create_reservation()
        elif (book_id := self._match_fulfill_path(self.path)) is not None:
            self._handle_fulfill_reservation(book_id)
        elif (book_id := self._match_defer_path(self.path)) is not None:
            self._handle_defer_reservation(book_id)
        elif (book_id := self._match_restock_path(self.path)) is not None:
            self._handle_restock_book(book_id)
        elif (loan_id := self._match_return_path(self.path)) is not None:
            self._handle_return_loan(loan_id)
        else:
            self._send_json(404, {"error": f"no route for POST {self.path}"})

    @staticmethod
    def _match_fulfill_path(path: str) -> str | None:
        """POST /books/<book_id>/reservations/fulfill → book_id (TC1)。"""
        parts = path.split("/")
        if (
            len(parts) == 5
            and parts[:2] == ["", "books"]
            and parts[3:] == ["reservations", "fulfill"]
        ):
            return parts[2] or None
        return None

    @staticmethod
    def _match_defer_path(path: str) -> str | None:
        """POST /books/<book_id>/reservations/defer → book_id (#42/TC1)。"""
        parts = path.split("/")
        if (
            len(parts) == 5
            and parts[:2] == ["", "books"]
            and parts[3:] == ["reservations", "defer"]
        ):
            return parts[2] or None
        return None

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
        elif (book_id := self._match_reservations_path(path)) is not None:
            self._handle_list_reservations(book_id)
        elif (book_id := self._match_book_path(path)) is not None:
            self._handle_get_book(book_id)
        else:
            self._send_json(404, {"error": f"no route for GET {self.path}"})

    @staticmethod
    def _match_reservations_path(path: str) -> str | None:
        """GET /books/<book_id>/reservations → book_id (TC1)。"""
        parts = path.split("/")
        if (
            len(parts) == 4
            and parts[:2] == ["", "books"]
            and parts[3] == "reservations"
        ):
            return parts[2] or None
        return None

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
        """POST /loans → 201 Loan / 404 未知 book / 409 有等待队列 / 409 库存耗尽。"""
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
        except QueuedForReservation as exc:
            self._send_json(
                409,
                {
                    "error": str(exc),
                    "code": RESERVATION_QUEUE_ACTIVE,
                    "next_action": exc.next_action,
                },
            )
        except Conflict as exc:
            self._send_json(409, {"error": str(exc)})
        else:
            self._send_json(201, loan)

    def _handle_create_reservation(self):
        """POST /reservations → 201 新建 / 200 复用 / 404 未知 book / 409 仍有库存。"""
        payload = self._read_json()
        if payload is None:
            return
        book_id = payload.get("book_id")
        holder = payload.get("holder")
        if not isinstance(book_id, str) or not book_id:
            self._send_json(400, {"error": "book_id must be a non-empty string"})
            return
        if not isinstance(holder, str) or not holder:
            self._send_json(400, {"error": "holder must be a non-empty string"})
            return
        try:
            reservation, created = create_reservation(book_id, holder)
        except NotFound as exc:
            self._send_json(404, {"error": str(exc)})
        except ReservationRejected as exc:
            self._send_json(409, {"error": str(exc)})
        else:
            self._send_json(201 if created else 200, reservation)

    def _handle_list_reservations(self, book_id: str):
        """GET /books/<id>/reservations → 200 等待预约数组 (TC5)。"""
        try:
            reservations = list_reservations(book_id)
        except NotFound as exc:
            self._send_json(404, {"error": str(exc)})
        else:
            self._send_json(200, reservations)

    def _handle_fulfill_reservation(self, book_id: str):
        """POST /books/<id>/reservations/fulfill → 201 {reservation, loan}

        404 未知 book / 409 没有等待者 / 409 没有可用库存 (TC1, DEC5, DEC6)；无请求体。
        """
        try:
            fulfillment = fulfill_head_reservation(book_id)
        except NotFound as exc:
            self._send_json(404, {"error": str(exc)})
        except NoWaitingReservation as exc:
            self._send_json(409, {"error": str(exc)})
        except Conflict as exc:
            self._send_json(409, {"error": str(exc)})
        else:
            self._send_json(201, fulfillment)

    def _handle_defer_reservation(self, book_id: str):
        """POST /books/<id>/reservations/defer → 200 被处置的那条等待预约

        404 未知 book / 409 没有等待者 (#42/TC1)；无请求体，响应中不含任何 Loan。
        """
        try:
            reservation = defer_head_reservation(book_id)
        except NotFound as exc:
            self._send_json(404, {"error": str(exc)})
        except NoWaitingReservation as exc:
            self._send_json(409, {"error": str(exc)})
        else:
            self._send_json(200, reservation)

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
