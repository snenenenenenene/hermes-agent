"""Delivery receipts — the record that separates "dispatched" from "landed".

``message_agent`` used to answer ``{"status": "sent"}`` for every dispatch, so a
message admitted into the recipient's context, one still queued behind a busy
profile, and one refused two minutes later with ``target_busy`` all read as the
same word. Measured 2026-09-15 on the orchestrator's own Bot Chat store: of 24
dispatches, 11 had a row in the target's store, 12 had bounced with a structured
"NOT delivered" payload (which only ever arrived as an unlabelled background-process
notification), and 1 had no signal at all — and all 24 answered ``"sent"``.

The fix is one idempotency key per delivery plus one durable receipt in the
TARGET's home: ``<home>/runtime/bot_delivery_receipts/<delivery_id>.json``. The
vocabulary is closed and each word is a different fact:

    dispatched   accepted for delivery; NOT yet admitted into the target's context
    landed       admitted — the target's own store carries the message (received_at)
    bounced      definitively NOT delivered, with the reason that stopped it
    unknown      the outcome could not be determined (killed runner, no row found)

Writers, one per transport, one row per delivery:

* the **sender** writes ``dispatched`` when it hands the delivery to a transport;
* the **runner** (``tools/bot_mode_dm.py --run-delivery``) writes the terminal
  receipt for a local Bot Chat turn — ``landed`` only after verifying the target's
  own store carries the message, ``bounced`` when the turn was refused;
* the **live mailbox** (``tools/bot_live_delivery.py``) keeps its own record and
  its own state machine (queued/claimed/settled/failed/cancelled/ambiguous); a
  reader maps those onto this vocabulary (see ``MAILBOX_STATUS``) so the sender
  sees exactly one language for both transports.

Terminal receipts are immutable: a second write with a different verdict is an
error, never an overwrite (same rule as the live mailbox).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from hermes_cli.active_sessions import _FileLock
from utils import atomic_json_write, fsync_directory

RECEIPTS_DIR_NAME = "bot_delivery_receipts"
BOT_CHAT_TITLE = "Bot Chat"

#: Closed sender-facing vocabulary. Nothing else may be reported as an outcome.
STATUSES = ("dispatched", "landed", "bounced", "unknown")
#: ``dispatched`` is the only non-terminal status: the delivery is still in flight.
TERMINAL_STATUSES = ("landed", "bounced", "unknown")

#: ``bot_live_delivery`` states → sender vocabulary. ``claimed`` and ``settled`` both
#: mean the target's owner took the message into its turn, so both are ``landed``;
#: the delivery's *reply* is a later, separate fact.
MAILBOX_STATUS: dict[str, str] = {
    "queued": "dispatched",
    "claimed": "landed",
    "settled": "landed",
    "failed": "bounced",
    "cancelled": "bounced",
    "ambiguous": "unknown",
}

_ID_RE = re.compile(r"^[0-9a-f]{32,64}$")
# How much of a delivered message is enough to recognise its row in the target's
# store. The prefix alone is unique per dispatch in practice; keeping it short
# keeps ``instr()`` cheap on a big transcript.
_MATCH_CHARS = 160


def new_delivery_id() -> str:
    """A fresh idempotency key (32 lowercase hex, the shape the live mailbox accepts)."""
    return uuid.uuid4().hex


def delivery_id_for_dm_file(path: str | os.PathLike[str]) -> str:
    """The delivery key of a dispatch: derived from its DM payload file, which
    ``mkstemp`` makes unique per dispatch and which a retry reuses verbatim — so a
    retry inspects the same receipt instead of minting a second delivery."""
    return hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()


def _valid_id(delivery_id: Any) -> str:
    if not isinstance(delivery_id, str) or not _ID_RE.fullmatch(delivery_id):
        raise ValueError("delivery id must be 32 to 64 lowercase hex characters")
    return delivery_id


def receipt_root(home: Path | str) -> Path:
    return Path(home).resolve() / "runtime" / RECEIPTS_DIR_NAME


@contextmanager
def _locked(home: Path | str):
    root = receipt_root(home)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    fsync_directory(root.parent)
    fsync_directory(root.parent.parent)
    lock = root / ".lock"
    fd = os.open(lock, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    with _FileLock(lock):
        yield root


def _read(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, OSError):  # a torn/absent receipt is "no receipt", never a crash
        return None


def _write(path: Path, record: dict[str, Any]) -> None:
    atomic_json_write(path, record, indent=None, sort_keys=True, fsync_dir=True, mode=0o600)


def write_receipt(
    home: Path | str,
    delivery_id: str,
    *,
    status: str,
    target: Optional[str] = None,
    sender: Optional[str] = None,
    transport: Optional[str] = None,
    detail: str = "",
    error: str = "",
    reason: str = "",
    session_id: Optional[str] = None,
    message_row_id: Optional[int] = None,
    received_at: Optional[float] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Upsert one delivery's receipt. ``dispatched`` may be refreshed; a terminal
    verdict is immutable (an identical re-write is a no-op, a different one raises)."""
    key = _valid_id(delivery_id)
    if status not in STATUSES:
        raise ValueError(f"unknown delivery status: {status!r}")
    stamp = time.time() if now is None else now
    with _locked(home) as root:
        path = root / f"{key}.json"
        existing = _read(path)
        if existing is not None and existing.get("status") in TERMINAL_STATUSES:
            if existing.get("status") != status:
                raise ValueError(
                    f"delivery {key} already has a terminal receipt "
                    f"({existing['status']}); refusing to overwrite it with {status!r}"
                )
            return existing
        record: dict[str, Any] = dict(existing or {})
        record.update(
            delivery_id=key, id=key, status=status,
            dispatched_at=record.get("dispatched_at", stamp),
            updated_at=stamp,
            terminal=status in TERMINAL_STATUSES,
        )
        for field, value in (("target", target), ("sender", sender), ("transport", transport),
                             ("detail", detail), ("error", error), ("reason", reason),
                             ("session_id", session_id), ("message_row_id", message_row_id)):
            if value:
                record[field] = value
        if status == "landed":
            record["received_at"] = received_at if received_at is not None else stamp
        _write(path, record)
        return record


def read_receipt(home: Path | str, delivery_id: str) -> Optional[dict[str, Any]]:
    """This delivery's own receipt, or None."""
    return _read(receipt_root(home) / f"{_valid_id(delivery_id)}.json")


def sender_status(home: Path | str, delivery_id: str) -> Optional[dict[str, Any]]:
    """The delivery's receipt as the sender needs to read it, whichever transport
    carried it: this store first, then the live mailbox record mapped onto the same
    vocabulary (with ``mailbox_status`` preserved for detail)."""
    record = read_receipt(home, delivery_id)
    if record is not None:
        return record
    try:
        from tools.bot_live_delivery import read_delivery_result

        live = read_delivery_result(home, delivery_id)
    except Exception:  # pragma: no cover — mailbox unavailable is "no receipt"
        return None
    if not live:
        return None
    mapped = MAILBOX_STATUS.get(live.get("status"), "unknown")
    if mapped == "landed":
        received_at = live.get("claimed_at") or live.get("completed_at") or live.get("created_at")
        # mailbox stamps are ns (time_ns); the sender-facing store speaks seconds.
        if isinstance(received_at, (int, float)) and received_at > 1e12:
            received_at = received_at / 1e9
    else:
        received_at = None
    return {
        "delivery_id": delivery_id, "id": delivery_id, "status": mapped,
        "mailbox_status": live.get("status"), "terminal": mapped in TERMINAL_STATUSES,
        "dispatched_at": (live.get("created_at") or 0) / 1e9 if live.get("created_at") else None,
        "received_at": received_at,
        "reply": live.get("reply") or "", "error": live.get("error") or "",
        "reason": live.get("reason") or "", "transport": "live",
    }


def wait_for_terminal(home: Path | str, delivery_id: str, *, timeout: float,
                      poll: float = 0.25) -> Optional[dict[str, Any]]:
    """Poll this delivery's receipt until it reaches a terminal status or *timeout*
    expires (returns None). Bounded by construction — a caller never waits on the
    recipient's *reply* this way."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        record = sender_status(home, delivery_id)
        if record is not None and record.get("terminal"):
            return record
        if time.monotonic() >= deadline:
            return record
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))


def latest_receipts(home: Path | str, limit: int = 20) -> list[dict[str, Any]]:
    """Newest-first receipts in one home (operator/audit view)."""
    root = receipt_root(home)
    if not root.is_dir():
        return []
    records = [r for r in (_read(p) for p in root.glob("*.json")) if r]
    records.sort(key=lambda r: r.get("dispatched_at") or 0, reverse=True)
    return records[:max(0, limit)]


def verify_admission(
    home: Path | str, text: str, *, since: Optional[float] = None,
    session_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Did the target's OWN store actually take this message? Returns the row that
    carries it (``session_id``, ``message_row_id``, ``received_at``) or None.

    Read-only, straight off the target's ``state.db``: a receipt is only worth
    anything if it is backed by the recipient's own record, so this reads the
    transcript rather than trusting the transport's exit code.
    """
    db_path = Path(home).resolve() / "state.db"
    if not db_path.is_file():
        return None
    needle = (text or "").strip()[:_MATCH_CHARS]
    if not needle:
        return None
    sql = ("SELECT m.id AS row_id, m.session_id AS session_id, m.timestamp AS ts "
           "FROM messages m JOIN sessions s ON s.id = m.session_id "
           "WHERE m.role = 'user' AND instr(COALESCE(m.content, ''), ?) > 0")
    params: list[Any] = [needle]
    if session_id:
        sql += " AND m.session_id = ?"
        params.append(session_id)
    if since is not None:
        sql += " AND m.timestamp >= ?"
        params.append(float(since) - 5.0)  # clock skew between dispatch and turn start
    sql += " ORDER BY m.timestamp ASC LIMIT 5"
    try:
        # Read-only URI: never create the file, never block the target's writer.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not rows:
        return None
    row = rows[0]
    return {"session_id": row["session_id"], "message_row_id": row["row_id"],
            "received_at": row["ts"], "matches": len(rows)}


def describe(record: Optional[dict[str, Any]]) -> str:
    """One plain line for a receipt — the operator-facing form."""
    if not record:
        return "no receipt"
    status = record.get("status", "unknown")
    parts = [status]
    if record.get("mailbox_status"):
        parts.append(f"(mailbox {record['mailbox_status']})")
    if status == "landed" and record.get("received_at"):
        parts.append(f"received_at {time.strftime('%H:%M:%S', time.localtime(record['received_at']))}")
    if record.get("reason"):
        parts.append(f"reason={record['reason']}")
    if record.get("error"):
        parts.append(f"error={record['error'][:120]}")
    return " ".join(parts)
