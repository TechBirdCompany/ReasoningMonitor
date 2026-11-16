#!/usr/bin/env python3
"""NiceGUI dashboard and passive OpenAI-compatible reasoning proxy.

The proxy only handles requests sent by a client configured to use it. It does
not generate, poll, or modify model requests. Streamed response bytes are passed
back to the client while reasoning and response text are copied into the UI.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import http.client
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import urlsplit

from nicegui import ui


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

MAX_MEMORY_REQUESTS = 80
MAX_UI_CARDS = 50
MAX_TEXT_CHARS_PER_FIELD = 500_000
RETENTION_DAYS = 7
CLEANUP_INTERVAL_SECONDS = 3600


def local_now() -> datetime:
    """Return the current local time with timezone information."""
    return datetime.now().astimezone()


def clean_text(value: str) -> str:
    """Normalize streamed text without changing Markdown or code formatting."""
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value


@dataclass
class RequestEntry:
    """Mutable state for one proxied model request."""

    request_id: str
    started_at: datetime
    model: str
    path: str
    client_ip: str
    status: str = "LIVE"
    http_status: int | None = None
    reasoning: str = ""
    answer: str = ""
    reasoning_chunks: int = 0
    answer_chunks: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    first_token_mono: float | None = None
    last_token_mono: float | None = None
    finished_mono: float | None = None
    generation_seconds: float = 0.0
    error: str = ""
    version: int = 0

    @property
    def estimated_tokens(self) -> int:
        """Estimate generated tokens from non-empty streamed deltas."""
        return self.completion_tokens if self.completion_tokens is not None else self.reasoning_chunks + self.answer_chunks

    @property
    def token_count_label(self) -> str:
        if self.completion_tokens is not None:
            return f"{self.completion_tokens:,} tokens"
        return f"~{self.reasoning_chunks + self.answer_chunks:,} streamed chunks"

    def tokens_per_second(self) -> float:
        """Return measured or estimated generation throughput."""
        count = self.estimated_tokens
        if count <= 0:
            return 0.0
        if self.first_token_mono is None:
            duration = self.generation_seconds
        elif self.finished_mono is not None:
            duration = self.generation_seconds
        else:
            end = max(self.last_token_mono or self.first_token_mono, time.monotonic())
            duration = end - self.first_token_mono
        if duration <= 0:
            return 0.0
        return count / duration

    @property
    def throughput_label(self) -> str:
        """Format throughput and distinguish exact usage from chunk estimates."""
        prefix = "" if self.completion_tokens is not None else "~"
        return f"{prefix}{self.tokens_per_second():.1f} tok/s"


class RequestStore:
    """Thread-safe in-memory request store with SQLite history."""

    def __init__(self, database_path: str) -> None:
        self.database_path = os.path.abspath(database_path)
        os.makedirs(os.path.dirname(self.database_path) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self.entries: dict[str, RequestEntry] = {}
        self.order: list[str] = []
        self._initialize_database()
        self.startup_deleted = self.cleanup_old()
        self._load_history()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a SQLite connection and always close it after the transaction."""
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    path TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    status TEXT NOT NULL,
                    http_status INTEGER,
                    reasoning TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    reasoning_chunks INTEGER NOT NULL,
                    answer_chunks INTEGER NOT NULL,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    generation_seconds REAL NOT NULL,
                    error TEXT NOT NULL
                )"""
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_requests_started ON requests(started_at)")

    def _load_history(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM requests ORDER BY started_at DESC LIMIT ?", (MAX_UI_CARDS,)
            ).fetchall()
        loaded: list[RequestEntry] = []
        for row in reversed(rows):
            try:
                started = datetime.fromisoformat(row["started_at"])
            except (TypeError, ValueError):
                started = local_now()
            entry = RequestEntry(
                request_id=row["request_id"],
                started_at=started,
                model=row["model"],
                path=row["path"],
                client_ip=row["client_ip"],
                status=row["status"],
                http_status=row["http_status"],
                reasoning=row["reasoning"],
                answer=row["answer"],
                reasoning_chunks=row["reasoning_chunks"],
                answer_chunks=row["answer_chunks"],
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                total_tokens=row["total_tokens"],
                generation_seconds=row["generation_seconds"],
                error=row["error"],
                version=1,
            )
            loaded.append(entry)
        with self.lock:
            for entry in loaded:
                self.entries[entry.request_id] = entry
                self.order.append(entry.request_id)

    def add_request(self, entry: RequestEntry) -> None:
        with self.lock:
            self.entries[entry.request_id] = entry
            self.order.append(entry.request_id)
            while len(self.order) > MAX_MEMORY_REQUESTS:
                old_id = self.order.pop(0)
                self.entries.pop(old_id, None)

    def append_text(self, request_id: str, channel: str, text: str) -> None:
        if not text:
            return
        with self.lock:
            entry = self.entries.get(request_id)
            if entry is None:
                return
            now = time.monotonic()
            if entry.first_token_mono is None:
                entry.first_token_mono = now
            entry.last_token_mono = now
            if channel == "reasoning":
                entry.reasoning += text
                entry.reasoning_chunks += 1
                if len(entry.reasoning) > MAX_TEXT_CHARS_PER_FIELD:
                    entry.reasoning = entry.reasoning[-MAX_TEXT_CHARS_PER_FIELD:]
            else:
                entry.answer += text
                entry.answer_chunks += 1
                if len(entry.answer) > MAX_TEXT_CHARS_PER_FIELD:
                    entry.answer = entry.answer[-MAX_TEXT_CHARS_PER_FIELD:]
            entry.version += 1

    def set_usage(self, request_id: str, usage: dict[str, Any]) -> None:
        with self.lock:
            entry = self.entries.get(request_id)
            if entry is None:
                return
            for source, target in (
                ("prompt_tokens", "prompt_tokens"),
                ("completion_tokens", "completion_tokens"),
                ("total_tokens", "total_tokens"),
            ):
                value = usage.get(source)
                if isinstance(value, int):
                    setattr(entry, target, value)
            entry.version += 1

    def finish(self, request_id: str, status: str, http_status: int | None = None, error: str = "") -> None:
        with self.lock:
            entry = self.entries.get(request_id)
            if entry is None:
                return
            entry.status = status
            entry.http_status = http_status
            entry.error = error
            entry.finished_mono = time.monotonic()
            if entry.first_token_mono is not None and entry.last_token_mono is not None:
                entry.generation_seconds = max(0.0, entry.last_token_mono - entry.first_token_mono)
            entry.version += 1
            snapshot = replace(entry)
        self._persist(snapshot)

    def _persist(self, entry: RequestEntry) -> None:
        """Persist a completed request unless it was removed by a reset."""
        with self.lock:
            if entry.request_id not in self.entries:
                return
            with self._connect() as connection:
                connection.execute(
                    """INSERT OR REPLACE INTO requests (
                        request_id, started_at, model, path, client_ip, status, http_status,
                        reasoning, answer, reasoning_chunks, answer_chunks, prompt_tokens,
                        completion_tokens, total_tokens, generation_seconds, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.request_id, entry.started_at.isoformat(), entry.model,
                        entry.path, entry.client_ip, entry.status, entry.http_status,
                        entry.reasoning, entry.answer, entry.reasoning_chunks,
                        entry.answer_chunks, entry.prompt_tokens, entry.completion_tokens,
                        entry.total_tokens, entry.generation_seconds, entry.error,
                    ),
                )

    def snapshot(self) -> list[RequestEntry]:
        with self.lock:
            return [replace(self.entries[key]) for key in self.order if key in self.entries]

    def clear(self) -> int:
        """Delete all saved requests and clear the in-memory request list."""
        with self.lock:
            active_count = sum(1 for entry in self.entries.values() if entry.status == "LIVE")
            with self._connect() as connection:
                stored_count = int(connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
                connection.execute("DELETE FROM requests")
            self.entries.clear()
            self.order.clear()
            return stored_count + active_count

    def cleanup_old(self, retention_days: int = RETENTION_DAYS) -> int:
        """Delete completed requests older than the configured retention period."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        with self.lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM requests WHERE julianday(started_at) < julianday(?)",
                    (cutoff.isoformat(),),
                )
                deleted_rows = max(0, cursor.rowcount)

            expired_ids: list[str] = []
            for request_id, entry in self.entries.items():
                # Never remove an active stream from memory because it is still in progress.
                if entry.status == "LIVE":
                    continue
                started_at = entry.started_at
                if started_at.tzinfo is None:
                    started_at = started_at.astimezone()
                if started_at.astimezone(timezone.utc) < cutoff:
                    expired_ids.append(request_id)

            for request_id in expired_ids:
                self.entries.pop(request_id, None)
            if expired_ids:
                expired_set = set(expired_ids)
                self.order = [request_id for request_id in self.order if request_id not in expired_set]

            return deleted_rows

    def stats(self) -> dict[str, Any]:
        entries = self.snapshot()
        today = local_now().date()
        today_key = today.isoformat()
        active_entries = [
            entry for entry in entries
            if entry.status == "LIVE" and entry.started_at.astimezone().date() == today
        ]
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT completion_tokens, reasoning_chunks, answer_chunks, generation_seconds
                   FROM requests WHERE substr(started_at, 1, 10) = ?""",
                (today_key,),
            ).fetchall()

        token_total = 0
        rates: list[float] = []
        for row in rows:
            token_count = row["completion_tokens"]
            if token_count is None:
                token_count = row["reasoning_chunks"] + row["answer_chunks"]
            token_total += token_count
            duration = row["generation_seconds"]
            if token_count > 0 and duration > 0:
                rates.append(token_count / duration)

        token_total += sum(entry.estimated_tokens for entry in active_entries)
        rates.extend(entry.tokens_per_second() for entry in active_entries if entry.estimated_tokens > 0)
        average_rate = sum(rates) / len(rates) if rates else 0.0
        return {
            "today_requests": len(rows) + len(active_entries),
            "active": sum(entry.status == "LIVE" for entry in entries),
            "tokens_today": token_total,
            "average_tps": average_rate,
            "requests": len(rows) + len(active_entries),
        }


class SSEParser:
    """Incrementally parse Server-Sent Events without changing forwarded bytes."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.data_lines: list[bytes] = []

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer.extend(data)
        events: list[bytes] = []
        while True:
            newline = self.buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self.buffer[:newline]).rstrip(b"\r")
            del self.buffer[: newline + 1]
            if not line:
                if self.data_lines:
                    events.append(b"\n".join(self.data_lines))
                    self.data_lines.clear()
            elif line.startswith(b"data:"):
                value = line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                self.data_lines.append(value)
        return events

    def finish(self) -> list[bytes]:
        events: list[bytes] = []
        if self.buffer:
            line = bytes(self.buffer).rstrip(b"\r")
            self.buffer.clear()
            if line.startswith(b"data:"):
                value = line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                self.data_lines.append(value)
        if self.data_lines:
            events.append(b"\n".join(self.data_lines))
            self.data_lines.clear()
        return events


class ReasoningProxyHandler(BaseHTTPRequestHandler):
    """Forward API requests and copy streamed text into the dashboard store."""

    protocol_version = "HTTP/1.1"
    server_version = "ReasoningMonitor/1.0"
    sys_version = ""

    def do_GET(self) -> None:
        self._proxy_request()

    def do_POST(self) -> None:
        self._proxy_request()

    def do_PUT(self) -> None:
        self._proxy_request()

    def do_PATCH(self) -> None:
        self._proxy_request()

    def do_DELETE(self) -> None:
        self._proxy_request()

    def do_OPTIONS(self) -> None:
        self._proxy_request()

    def do_HEAD(self) -> None:
        self._proxy_request()

    def _read_request_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in transfer_encoding:
            body = bytearray()
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    raise ConnectionError("Unexpected end of chunked request")
                try:
                    size = int(size_line.split(b";", 1)[0].strip(), 16)
                except ValueError as exc:
                    raise ConnectionError("Invalid request chunk size") from exc
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise ConnectionError("Incomplete chunked request")
                body.extend(chunk)
            return bytes(body)
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0:
            raise ValueError("Invalid Content-Length")
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            raise ConnectionError("Incomplete request body")
        return body

    def _proxy_request(self) -> None:
        request_id = uuid.uuid4().hex
        upstream = self.server.upstream
        connection: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        headers_sent = False
        response_status: int | None = None
        model = "unknown model"
        body = b""
        entry: RequestEntry | None = None

        try:
            body = self._read_request_body()
            try:
                request_json = json.loads(body) if body else {}
                if isinstance(request_json, dict):
                    model = str(request_json.get("model") or "unknown model")
            except (json.JSONDecodeError, UnicodeDecodeError):
                request_json = {}

            entry = RequestEntry(
                request_id=request_id,
                started_at=local_now(),
                model=model,
                path=self.path,
                client_ip=self.client_address[0] if self.client_address else "unknown",
            )
            self.server.store.add_request(entry)

            outgoing_headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS
            }
            outgoing_headers["Accept-Encoding"] = "identity"

            port = upstream.port or (443 if upstream.scheme == "https" else 80)
            if upstream.scheme == "https":
                connection = http.client.HTTPSConnection(upstream.hostname, port, timeout=self.server.timeout)
            else:
                connection = http.client.HTTPConnection(upstream.hostname, port, timeout=self.server.timeout)

            target = self.path
            if upstream.path and upstream.path != "/":
                target = upstream.path.rstrip("/") + (self.path if self.path.startswith("/") else "/" + self.path)

            connection.request(self.command, target, body=body, headers=outgoing_headers)
            response = connection.getresponse()
            response_status = response.status
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP_BY_HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            self.close_connection = True

            content_type = response.getheader("Content-Type", "").lower()
            is_sse = "text/event-stream" in content_type
            parser = SSEParser() if is_sse else None
            non_stream_body = bytearray()

            if self.command != "HEAD":
                while True:
                    data = response.read1(8192) if hasattr(response, "read1") else response.read(8192)
                    if not data:
                        break
                    self.wfile.write(data)
                    self.wfile.flush()

                    if parser is not None:
                        for event_payload in parser.feed(data):
                            self._process_event(request_id, event_payload)
                    else:
                        if len(non_stream_body) < 8_000_000:
                            non_stream_body.extend(data)

            if parser is not None:
                for event_payload in parser.finish():
                    self._process_event(request_id, event_payload)
            elif non_stream_body:
                self._process_non_stream_response(request_id, bytes(non_stream_body))

            status = "COMPLETE" if response.status < 400 else "ERROR"
            self.server.store.finish(request_id, status, response.status)

        except (OSError, http.client.HTTPException, ConnectionError, ValueError) as exc:
            if entry is not None:
                self.server.store.finish(request_id, "ERROR", response_status, str(exc))
            if not headers_sent:
                try:
                    self.send_error(502, f"Upstream error: {exc}")
                except OSError:
                    pass
        finally:
            if connection is not None:
                connection.close()
            self.close_connection = True

    def _process_event(self, request_id: str, payload: bytes) -> None:
        if not payload or payload == b"[DONE]":
            return
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(event, dict):
            return
        usage = event.get("usage")
        if isinstance(usage, dict):
            self.server.store.set_usage(request_id, usage)
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            reasoning = delta.get("reasoning_content")
            if reasoning is None:
                reasoning = delta.get("reasoning")
            if isinstance(reasoning, str):
                self.server.store.append_text(request_id, "reasoning", reasoning)
            content = delta.get("content")
            if isinstance(content, str):
                self.server.store.append_text(request_id, "answer", content)

    def _process_non_stream_response(self, request_id: str, payload: bytes) -> None:
        try:
            response_json = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(response_json, dict):
            return
        usage = response_json.get("usage")
        if isinstance(usage, dict):
            self.server.store.set_usage(request_id, usage)
        for choice in response_json.get("choices") or []:
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            content = message.get("content")
            if isinstance(reasoning, str):
                self.server.store.append_text(request_id, "reasoning", reasoning)
            if isinstance(content, str):
                self.server.store.append_text(request_id, "answer", content)

    def log_message(self, format_string: str, *args: object) -> None:
        return


class ReasoningProxyServer(ThreadingHTTPServer):
    """Threaded HTTP server holding the upstream and shared request store."""

    daemon_threads = True
    allow_reuse_address = True


def format_entry_time(value: datetime) -> str:
    """Format request timestamps for the dashboard."""
    return value.astimezone().strftime("%d.%m.%Y · %H:%M:%S")


def build_dashboard(store: RequestStore, proxy_port: int, upstream_url: str) -> None:
    """Register the NiceGUI dashboard page."""
    ui.colors(primary="#8b5cf6", secondary="#64748b", accent="#22d3ee", positive="#22c55e")
    ui.add_head_html(
        """<style>
        body { background: #0b1020; }
        .app-shell { max-width: 1500px; margin: 0 auto; }
        .stat-card { background: linear-gradient(145deg, #151d31, #101728); border: 1px solid #27334a; }
        .request-card { background: #111a2b; border: 1px solid #26334a; }
        .thinking-panel { border-left: 3px solid #8b5cf6; }
        .answer-panel { border-left: 3px solid #22c55e; }
        .muted { color: #9aa8bd; }
        .prose pre { background: #080d18 !important; border: 1px solid #27334a; border-radius: 10px; padding: 12px; overflow-x: auto; }
        .prose code { color: #c4b5fd; }
        .prose p { margin: .45rem 0; }
        .q-expansion-item { border-radius: 10px; }
        </style>""",
        shared=True,
    )

    ui.dark_mode().enable()
    card_widgets: dict[str, dict[str, Any]] = {}
    rendered_versions: dict[str, int] = {}

    with ui.column().classes("w-full app-shell p-4 md:p-6 gap-5"):
        with ui.row().classes("w-full items-center justify-between flex-wrap gap-3"):
            with ui.row().classes("items-center gap-3"):
                ui.icon("psychology", size="2.5rem").classes("text-violet-400")
                with ui.column().classes("gap-0"):
                    ui.label("Qwen Reasoning Monitor").classes("text-2xl md:text-3xl font-bold text-white")
                    ui.label("Live capture · request history · throughput statistics").classes("text-sm muted")
            with ui.column().classes("items-end gap-1"):
                ui.badge(f"PROXY :{proxy_port} · ONLINE", color="positive").classes("text-sm px-3 py-1")
                ui.label(f"Upstream  {upstream_url}").classes("text-xs muted")

        stat_values: dict[str, Any] = {}
        with ui.grid(columns=4).classes("w-full gap-3"):
            for key, title, icon_name in (
                ("today_requests", "Requests today", "forum"),
                ("active", "Active streams", "sensors"),
                ("tokens_today", "Generated tokens", "token"),
                ("average_tps", "Average throughput", "speed"),
            ):
                with ui.card().classes("stat-card w-full p-4"):
                    with ui.row().classes("items-center justify-between w-full"):
                        ui.label(title).classes("text-sm muted")
                        ui.icon(icon_name).classes("text-slate-400")
                    value = ui.label("0").classes("text-2xl font-semibold text-white")
                    stat_values[key] = value
                    if key == "tokens_today":
                        ui.label("Exact when API usage is supplied; otherwise estimated").classes("text-[10px] muted")
                    elif key == "average_tps":
                        ui.label("Estimated from streamed output chunks unless usage is supplied").classes("text-[10px] muted")

        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label("Captured requests").classes("text-xl font-semibold text-white")
                ui.label("Newest requests appear at the top. Entries older than 7 days are deleted automatically.").classes("text-xs muted")
            history_label = ui.label("0 requests loaded").classes("text-xs muted")
            with ui.dialog() as clear_dialog:
                with ui.card().classes("w-[28rem] max-w-full"):
                    ui.label("Reset captured history?").classes("text-lg font-semibold")
                    ui.label(
                        "This permanently deletes saved reasoning, final responses, and request statistics. "
                        "New requests will be captured normally. Active requests at the moment of reset "
                        "will be discarded from the monitor."
                    ).classes("text-sm muted")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Cancel", on_click=clear_dialog.close).props("flat")
                        ui.button(
                            "Delete history", icon="delete_forever", color="negative",
                            on_click=lambda: clear_history(),
                        )
            ui.button(
                "Reset history", icon="delete_sweep", color="negative",
                on_click=clear_dialog.open,
            ).props("outline")
        empty_state = ui.card().classes("w-full request-card p-8 items-center")
        with empty_state:
            ui.icon("hourglass_empty", size="2rem").classes("text-slate-500")
            ui.label("Waiting for the first Copilot request").classes("text-base text-slate-300")
            ui.label(f"Configure the Qwen endpoint to http://<server-ip>:{proxy_port}/v1").classes("text-xs muted")

        feed = ui.column().classes("w-full gap-3").style("flex-direction: column-reverse;")

        def create_request_card(entry: RequestEntry) -> dict[str, Any]:
            with feed:
                with ui.card().classes("w-full request-card p-4 gap-3") as request_card:
                    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                        with ui.column().classes("gap-0"):
                            time_label = ui.label(format_entry_time(entry.started_at)).classes("font-semibold text-white")
                            model_label = ui.label(f"{entry.model} · {entry.client_ip} · {entry.path}").classes("text-xs muted")
                        with ui.row().classes("items-center gap-2"):
                            rate_label = ui.label("Waiting for tokens").classes("text-xs muted")
                            status_badge = ui.badge(entry.status, color="positive" if entry.status == "COMPLETE" else "warning")
                    with ui.expansion("Thinking", icon="psychology", value=True).classes("w-full thinking-panel") as thinking_expansion:
                        thinking_meta = ui.label("No reasoning received yet").classes("text-xs muted")
                        thinking_md = ui.markdown("*Waiting for reasoning content…*").classes("w-full prose text-sm")
                    with ui.expansion("Final response", icon="chat", value=False).classes("w-full answer-panel"):
                        answer_md = ui.markdown("*Waiting for response content…*").classes("w-full prose text-sm")
                    error_label = ui.label("").classes("text-sm text-red-300")
            return {
                "card": request_card,
                "time": time_label,
                "model": model_label,
                "rate": rate_label,
                "status": status_badge,
                "thinking_meta": thinking_meta,
                "thinking": thinking_md,
                "thinking_expansion": thinking_expansion,
                "answer": answer_md,
                "error": error_label,
            }

        def refresh_dashboard() -> None:
            stats = store.stats()
            stat_values["today_requests"].set_text(f"{stats['today_requests']:,}")
            stat_values["active"].set_text(f"{stats['active']:,}")
            stat_values["tokens_today"].set_text(f"{stats['tokens_today']:,}")
            stat_values["average_tps"].set_text(f"{stats['average_tps']:.1f} tok/s")

            entries = store.snapshot()[-MAX_UI_CARDS:]
            visible_ids = {entry.request_id for entry in entries}
            for old_id in list(card_widgets):
                if old_id not in visible_ids:
                    card_widgets[old_id]["card"].delete()
                    del card_widgets[old_id]
                    rendered_versions.pop(old_id, None)
            history_label.set_text(f"{len(entries)} requests in view")
            empty_state.set_visibility(not entries)

            for entry in entries:
                widgets = card_widgets.get(entry.request_id)
                if widgets is None:
                    widgets = create_request_card(entry)
                    card_widgets[entry.request_id] = widgets
                if rendered_versions.get(entry.request_id) == entry.version:
                    continue

                widgets["time"].set_text(format_entry_time(entry.started_at))
                widgets["model"].set_text(f"{entry.model} · {entry.client_ip} · {entry.path}")
                if entry.status == "LIVE":
                    widgets["status"].set_text("LIVE")
                    widgets["rate"].set_text(f"{entry.throughput_label} · {entry.token_count_label}")
                else:
                    color = "positive" if entry.status == "COMPLETE" else "negative"
                    widgets["status"].set_text(entry.status)
                    widgets["status"].props(f"color={color}")
                    widgets["rate"].set_text(
                        f"{entry.throughput_label} · {entry.token_count_label}"
                        + (f" · HTTP {entry.http_status}" if entry.http_status else "")
                    )

                reasoning_text = clean_text(entry.reasoning)
                if reasoning_text:
                    widgets["thinking"].set_content(reasoning_text)
                    widgets["thinking_meta"].set_text(
                        f"{entry.reasoning_chunks:,} streamed reasoning chunks"
                        + (f" · {entry.prompt_tokens:,} prompt tokens" if entry.prompt_tokens is not None else "")
                    )
                else:
                    widgets["thinking"].set_content("*No reasoning content received for this request.*")
                    widgets["thinking_meta"].set_text("No reasoning field received")

                answer_text = clean_text(entry.answer)
                widgets["answer"].set_content(answer_text or "*No final response content received yet.*")
                widgets["error"].set_text(entry.error)
                rendered_versions[entry.request_id] = entry.version

        def clear_history() -> None:
            """Clear persisted and in-memory history, then refresh the dashboard."""
            deleted_count = store.clear()
            clear_dialog.close()
            refresh_dashboard()
            ui.notify(
                f"Deleted {deleted_count} captured requests. Statistics have been reset.",
                type="positive",
            )

        refresh_dashboard()
        ui.timer(0.10, refresh_dashboard)

def retention_cleanup_worker(store: RequestStore, stop_event: threading.Event) -> None:
    """Prune expired history periodically without depending on dashboard activity."""
    while not stop_event.wait(CLEANUP_INTERVAL_SECONDS):
        try:
            deleted = store.cleanup_old()
            if deleted:
                print(f"[retention] Removed {deleted} requests older than {RETENTION_DAYS} days.", flush=True)
        except Exception as exc:
            print(f"[retention] Cleanup failed: {exc}", file=sys.stderr, flush=True)


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the proxy and dashboard."""
    parser = argparse.ArgumentParser(
        description="Passively capture Qwen reasoning from API requests and display it in a NiceGUI dashboard."
    )
    parser.add_argument("--proxy-host", default="0.0.0.0", help="Proxy bind address (default: 0.0.0.0)")
    parser.add_argument("--proxy-port", type=int, default=4001, help="Proxy port (default: 4001)")
    parser.add_argument(
        "--upstream",
        default="http://10.10.10.22:4000",
        help="Upstream OpenAI-compatible API base URL",
    )
    parser.add_argument("--web-host", default="0.0.0.0", help="Dashboard bind address (default: 0.0.0.0)")
    parser.add_argument("--web-port", type=int, default=8088, help="Dashboard port (default: 8088)")
    parser.add_argument(
        "--db",
        default="./reasoning_monitor.sqlite3",
        help="SQLite history database path (default: ./reasoning_monitor.sqlite3)",
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="Upstream timeout in seconds")
    return parser.parse_args()


def main() -> int:
    """Start the passive API proxy and the NiceGUI dashboard."""
    args = parse_args()
    upstream = urlsplit(args.upstream)
    if upstream.scheme not in ("http", "https") or not upstream.hostname:
        print("Error: --upstream must be an http:// or https:// URL", file=sys.stderr)
        return 2
    if upstream.query or upstream.fragment:
        print("Error: --upstream must not contain a query string or fragment", file=sys.stderr)
        return 2
    if not (1 <= args.proxy_port <= 65535 and 1 <= args.web_port <= 65535):
        print("Error: ports must be between 1 and 65535", file=sys.stderr)
        return 2

    store = RequestStore(args.db)
    cleanup_stop = threading.Event()
    cleanup_thread = threading.Thread(
        target=retention_cleanup_worker, args=(store, cleanup_stop),
        name="history-retention", daemon=True,
    )
    cleanup_thread.start()
    proxy = ReasoningProxyServer((args.proxy_host, args.proxy_port), ReasoningProxyHandler)
    proxy.upstream = upstream
    proxy.timeout = args.timeout
    proxy.store = store

    proxy_thread = threading.Thread(target=proxy.serve_forever, name="reasoning-proxy", daemon=True)
    proxy_thread.start()

    upstream_display = args.upstream.rstrip("/")

    def dashboard_root() -> None:
        """Build a fresh dashboard for each browser connection."""
        build_dashboard(store, args.proxy_port, upstream_display)

    print("Qwen Reasoning Monitor", flush=True)
    print(f"API proxy:  http://{args.proxy_host}:{args.proxy_port}/v1", flush=True)
    print(f"Dashboard:  http://<server-ip>:{args.web_port} (bind: {args.web_host})", flush=True)
    print(f"Upstream:   {upstream_display}", flush=True)
    print(f"History DB: {os.path.abspath(args.db)}", flush=True)
    print(f"History retention: {RETENTION_DAYS} days (removed {store.startup_deleted} expired requests at startup)", flush=True)
    print("The proxy is passive: it only observes requests sent by the configured client.", flush=True)

    try:
        ui.run(
            root=dashboard_root,
            host=args.web_host,
            port=args.web_port,
            title="Qwen Reasoning Monitor",
            dark=True,
            show=False,
            reload=False,
        )
    finally:
        cleanup_stop.set()
        proxy.shutdown()
        proxy.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
