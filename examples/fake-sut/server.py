"""Tiny in-memory Todos server for the RC proof fixture (issue #137).

Stdlib HTTP server — no dependencies — implementing the contract from
``openapi.yaml`` just well enough that the online half of the proof
(``run-rc-proof.sh --online``) can run generated tests against it.

Run directly:
    python examples/fake-sut/server.py [--port 8001] [--token secret]

The offline half of the RC proof never starts this server; it only
exercises the analyse + plan stage, which is deterministic.
"""
from __future__ import annotations

import argparse
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any, Dict, List


_TODO_PATH = re.compile(r"^/todos/(?P<id>-?\d+)$")


class _Store:
    def __init__(self) -> None:
        self._lock = Lock()
        self._next_id = 1
        self._items: Dict[int, Dict[str, Any]] = {}

    def list(self, completed: Any = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._items.values())
        if completed is None:
            return items
        return [t for t in items if t["completed"] == completed]

    def create(self, title: str, completed: bool) -> Dict[str, Any]:
        with self._lock:
            todo = {"id": self._next_id, "title": title, "completed": completed}
            self._items[self._next_id] = todo
            self._next_id += 1
            return dict(todo)

    def get(self, todo_id: int) -> Dict[str, Any] | None:
        with self._lock:
            todo = self._items.get(todo_id)
            return dict(todo) if todo else None

    def replace(self, todo_id: int, title: str, completed: bool) -> Dict[str, Any] | None:
        with self._lock:
            if todo_id not in self._items:
                return None
            todo = {"id": todo_id, "title": title, "completed": completed}
            self._items[todo_id] = todo
            return dict(todo)

    def delete(self, todo_id: int) -> bool:
        with self._lock:
            return self._items.pop(todo_id, None) is not None


STORE = _Store()
EXPECTED_TOKEN = "secret"


def _validate_title(title: Any) -> str | None:
    if not isinstance(title, str) or not title:
        return "title must be a non-empty string"
    if len(title) > 200:
        return "title exceeds 200 characters"
    return None


def _parse_completed(query: str) -> bool | None:
    for part in query.split("&"):
        if part.startswith("completed="):
            val = part.split("=", 1)[1].lower()
            if val in ("true", "1"):
                return True
            if val in ("false", "0"):
                return False
            return None
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeTodos/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _send(self, status: int, payload: Any | None = None) -> None:
        body = b""
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Content-Length", "0")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _auth_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {EXPECTED_TOKEN}"

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _INVALID

    def _split_path(self) -> tuple[str, str]:
        if "?" in self.path:
            return tuple(self.path.split("?", 1))  # type: ignore[return-value]
        return self.path, ""

    def do_GET(self) -> None:  # noqa: N802
        path, query = self._split_path()
        if path == "/todos":
            completed = _parse_completed(query)
            if query and "completed=" in query and completed is None:
                return self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_completed"})
            return self._send(HTTPStatus.OK, STORE.list(completed))
        match = _TODO_PATH.match(path)
        if match:
            try:
                todo_id = int(match.group("id"))
            except ValueError:
                return self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_id"})
            todo = STORE.get(todo_id)
            if todo is None:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return self._send(HTTPStatus.OK, todo)
        return self._send(HTTPStatus.NOT_FOUND, {"error": "unknown_route"})

    def do_POST(self) -> None:  # noqa: N802
        path, _ = self._split_path()
        if path != "/todos":
            return self._send(HTTPStatus.NOT_FOUND, {"error": "unknown_route"})
        if not self._auth_ok():
            return self._send(HTTPStatus.UNAUTHORIZED, {"error": "auth_required"})
        body = self._read_json()
        if body is _INVALID or not isinstance(body, dict):
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        err = _validate_title(body.get("title"))
        if err:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": err})
        todo = STORE.create(body["title"], bool(body.get("completed", False)))
        return self._send(HTTPStatus.CREATED, todo)

    def do_PUT(self) -> None:  # noqa: N802
        path, _ = self._split_path()
        match = _TODO_PATH.match(path)
        if not match:
            return self._send(HTTPStatus.NOT_FOUND, {"error": "unknown_route"})
        if not self._auth_ok():
            return self._send(HTTPStatus.UNAUTHORIZED, {"error": "auth_required"})
        try:
            todo_id = int(match.group("id"))
        except ValueError:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_id"})
        body = self._read_json()
        if body is _INVALID or not isinstance(body, dict):
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
        err = _validate_title(body.get("title"))
        if err:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": err})
        updated = STORE.replace(todo_id, body["title"], bool(body.get("completed", False)))
        if updated is None:
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return self._send(HTTPStatus.OK, updated)

    def do_DELETE(self) -> None:  # noqa: N802
        path, _ = self._split_path()
        match = _TODO_PATH.match(path)
        if not match:
            return self._send(HTTPStatus.NOT_FOUND, {"error": "unknown_route"})
        if not self._auth_ok():
            return self._send(HTTPStatus.UNAUTHORIZED, {"error": "auth_required"})
        try:
            todo_id = int(match.group("id"))
        except ValueError:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_id"})
        if not STORE.delete(todo_id):
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return self._send(HTTPStatus.NO_CONTENT)


class _Invalid:
    pass


_INVALID = _Invalid()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--token", default="secret")
    args = parser.parse_args()
    global EXPECTED_TOKEN
    EXPECTED_TOKEN = args.token
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake-todos server on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
