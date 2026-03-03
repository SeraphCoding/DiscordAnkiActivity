"""Discord Rich Presence for Anki deck activity."""

from __future__ import annotations

import json
import os
import socket
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from aqt import gui_hooks, mw
from aqt.qt import QTimer
from aqt.utils import showWarning

OPCODE_HANDSHAKE = 0
OPCODE_FRAME = 1
OPCODE_CLOSE = 2


@dataclass
class PresenceConfig:
    client_id: str
    update_interval_seconds: int = 15


DEFAULT_CONFIG = {
    "discord_client_id": "1478451530653765753",
    "update_interval_seconds": 15,
    "large_image": "anki",
    "large_text": "Studying with Anki",
}


class DiscordIPC:
    """Minimal Discord RPC IPC client implementation (no external dependency)."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.sock: Optional[socket.socket] = None
        self._pipe = None  # Windows named pipe handle

    def _candidate_paths(self) -> list[str]:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        run_user_dir = f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else None
        base_dirs = [p for p in [runtime_dir, "/tmp", "/var/tmp", run_user_dir] if p]
        candidates: list[str] = []
        for base_dir in base_dirs:
            for index in range(10):
                candidates.append(str(Path(base_dir) / f"discord-ipc-{index}"))
        return candidates

    def _candidate_pipe_names(self) -> list[str]:
        """Named pipe paths for Windows."""
        return [f"\\\\?\\pipe\\discord-ipc-{i}" for i in range(10)]

    def _send_packet(self, opcode: int, payload: dict[str, Any]) -> None:
        if self.sock is None and self._pipe is None:
            raise RuntimeError("Discord IPC socket is not connected")

        body = json.dumps(payload).encode("utf-8")
        header = struct.pack("<ii", opcode, len(body))
        data = header + body

        if self.sock is not None:
            self.sock.sendall(data)
        else:
            self._pipe_write(data)

    def _recv_packet(self) -> tuple[int, dict[str, Any]]:
        if self.sock is None and self._pipe is None:
            raise RuntimeError("Discord IPC socket is not connected")

        header = self._read_bytes(8)
        if len(header) != 8:
            raise RuntimeError("Invalid Discord IPC response header")

        opcode, length = struct.unpack("<ii", header)
        body = self._read_bytes(length)

        payload = json.loads(body.decode("utf-8"))
        return opcode, payload

    def _read_bytes(self, n: int) -> bytes:
        if self.sock is not None:
            data = b""
            while len(data) < n:
                chunk = self.sock.recv(n - len(data))
                if not chunk:
                    raise RuntimeError("Discord IPC socket closed unexpectedly")
                data += chunk
            return data
        else:
            return self._pipe_read(n)

    def _pipe_write(self, data: bytes) -> None:
        import ctypes
        import ctypes.wintypes
        kernel32 = ctypes.windll.kernel32
        written = ctypes.wintypes.DWORD()
        kernel32.WriteFile(self._pipe, data, len(data), ctypes.byref(written), None)

    def _pipe_read(self, n: int) -> bytes:
        import ctypes
        import ctypes.wintypes
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(n)
        read = ctypes.wintypes.DWORD()
        kernel32.ReadFile(self._pipe, buf, n, ctypes.byref(read), None)
        return buf.raw[: read.value]

    def connect(self) -> None:
        last_error: Optional[Exception] = None
        self._pipe = None

        if os.name == "nt":
            # Windows: use named pipes
            import ctypes
            kernel32 = ctypes.windll.kernel32
            GENERIC_READ_WRITE = 0xC0000000
            OPEN_EXISTING = 3
            INVALID_HANDLE = -1

            for pipe_name in self._candidate_pipe_names():
                try:
                    handle = kernel32.CreateFileW(
                        pipe_name,
                        GENERIC_READ_WRITE,
                        0,
                        None,
                        OPEN_EXISTING,
                        0,
                        None,
                    )
                    if handle == INVALID_HANDLE:
                        raise OSError(f"Cannot open pipe {pipe_name}")
                    self._pipe = handle
                    self._send_packet(
                        OPCODE_HANDSHAKE,
                        {"v": 1, "client_id": self.client_id},
                    )
                    self._recv_packet()
                    return
                except Exception as exc:
                    last_error = exc
                    if self._pipe and self._pipe != INVALID_HANDLE:
                        try:
                            kernel32.CloseHandle(self._pipe)
                        except Exception:
                            pass
                        self._pipe = None
        else:
            # Unix: use AF_UNIX sockets
            for path in self._candidate_paths():
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(path)
                    self.sock = sock
                    self._send_packet(
                        OPCODE_HANDSHAKE,
                        {"v": 1, "client_id": self.client_id},
                    )
                    self._recv_packet()
                    return
                except Exception as exc:
                    last_error = exc
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                        self.sock = None

        raise RuntimeError(f"Unable to connect to Discord IPC socket ({last_error})")

    def update(self, activity_payload: dict[str, Any]) -> None:
        command = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity_payload},
            "nonce": str(uuid.uuid4()),
        }
        self._send_packet(OPCODE_FRAME, command)
        self._recv_packet()

    def clear(self) -> None:
        command = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": None},
            "nonce": str(uuid.uuid4()),
        }
        self._send_packet(OPCODE_FRAME, command)
        self._recv_packet()

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        if self._pipe:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self._pipe)
            except Exception:
                pass
            self._pipe = None


class DiscordActivity:
    def __init__(self) -> None:
        self.rpc: Optional[DiscordIPC] = None
        self.timer: Optional[QTimer] = None
        self.last_payload: Optional[dict] = None
        self.started_at = int(time.time())
        self.large_image = DEFAULT_CONFIG["large_image"]
        self.large_text = DEFAULT_CONFIG["large_text"]

    def _load_config(self) -> PresenceConfig:
        config = mw.addonManager.getConfig(__name__) or {}
        merged = {**DEFAULT_CONFIG, **config}
        self.large_image = merged["large_image"]
        self.large_text = merged["large_text"]
        return PresenceConfig(
            client_id=str(merged["discord_client_id"]),
            update_interval_seconds=max(5, int(merged["update_interval_seconds"])),
        )

    def start(self) -> None:
        config = self._load_config()
        self.rpc = DiscordIPC(config.client_id)

        try:
            self.rpc.connect()
        except Exception as exc:  # pragma: no cover - runtime integration
            showWarning(f"Discord Anki Activity: failed to connect to Discord ({exc}).")
            self.rpc = None
            return

        self.timer = QTimer(mw)
        self.timer.timeout.connect(self.update_presence)
        self.timer.start(config.update_interval_seconds * 1000)
        self.update_presence()

    def stop(self) -> None:
        if self.timer:
            self.timer.stop()
            self.timer = None

        if self.rpc:
            try:
                self.rpc.clear()
                self.rpc.close()
            except Exception:
                pass
            self.rpc = None

    def _get_current_deck_name(self) -> str:
        deck = mw.col.decks.current()
        return deck.get("name", "Unknown Deck")

    def _get_queue_counts(self) -> tuple[int, int, int]:
        new_count, learning_count, review_count = mw.col.sched.counts()
        return int(new_count), int(learning_count), int(review_count)

    def _build_payload(self) -> dict:
        deck_name = self._get_current_deck_name()
        new_count, learning_count, review_count = self._get_queue_counts()

        return {
            "details": f"Studying with Anki, on Deck: {deck_name}",
            "state": f"New {new_count} | Learn {learning_count} | Review {review_count}",
            "timestamps": {"start": self.started_at},
            "assets": {
                "large_image": self.large_image,
                "large_text": self.large_text,
            },
        }

    def update_presence(self, *_args) -> None:
        if not self.rpc or not mw.col:
            return

        try:
            payload = self._build_payload()
            if payload != self.last_payload:
                self.rpc.update(payload)
                self.last_payload = payload
        except Exception:
            # Discord might restart; avoid surfacing noisy dialogs while studying.
            pass


activity = DiscordActivity()


def on_profile_open() -> None:
    activity.start()


def on_profile_close() -> None:
    activity.stop()


def on_state_change(*_args) -> None:
    activity.update_presence()


def on_reviewer_event(*_args) -> None:
    activity.update_presence()


gui_hooks.profile_did_open.append(on_profile_open)
gui_hooks.profile_will_close.append(on_profile_close)
gui_hooks.state_did_change.append(on_state_change)
gui_hooks.reviewer_did_show_question.append(on_reviewer_event)