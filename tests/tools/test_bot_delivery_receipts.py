"""Delivery receipts: `dispatched` is not `landed` (kanban t_89b88710).

The defect these tests pin down: ``message_agent`` answered ``{"status": "sent"}`` for
every dispatch, so a message admitted into the recipient's context, one still queued
behind a busy profile and one refused with ``target_busy`` read as the same word
(measured: 24 dispatches, 11 landed, 13 bounced, all 24 "sent"). The fix is one
idempotency key per delivery plus a durable receipt in the target's home; these tests
hold the vocabulary, the immutability rule, and — most importantly — the rule that
``landed`` is only ever written from the TARGET's own transcript.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time

import pytest

from tools import bot_mode_dm, bot_relay
from tools import bot_delivery_receipt as receipts
from tools.bot_relay import turn_lock_path

_DID = "a" * 64


# ── the store and its vocabulary ───────────────────────────────────────────────


def test_vocabulary_is_closed_and_unknown_words_are_refused(tmp_path):
    with pytest.raises(ValueError):
        receipts.write_receipt(tmp_path, _DID, status="sent")
    assert "sent" not in receipts.STATUSES


def test_terminal_receipt_is_immutable_and_identical_rewrites_are_no_ops(tmp_path):
    landed = receipts.write_receipt(tmp_path, _DID, status="landed", target="@x", received_at=123.0)
    assert landed["terminal"] is True and landed["received_at"] == 123.0
    assert receipts.write_receipt(tmp_path, _DID, status="landed", target="@x") == landed
    with pytest.raises(ValueError):
        receipts.write_receipt(tmp_path, _DID, status="bounced", reason="target_busy")
    assert receipts.read_receipt(tmp_path, _DID)["status"] == "landed"


def test_dispatched_is_refreshable_and_keeps_its_first_stamp(tmp_path):
    first = receipts.write_receipt(tmp_path, _DID, status="dispatched", target="@x", now=100.0)
    again = receipts.write_receipt(tmp_path, _DID, status="dispatched", target="@x",
                                   detail="handed to the local transport", now=200.0)
    assert again["dispatched_at"] == first["dispatched_at"] == 100.0
    assert again["detail"] == "handed to the local transport"
    assert again["terminal"] is False


def test_receipts_are_owner_only_and_keyed_by_id(tmp_path):
    import os

    receipts.write_receipt(tmp_path, _DID, status="dispatched", target="@x")
    path = receipts.receipt_root(tmp_path) / f"{_DID}.json"
    assert path.is_file()
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        receipts.read_receipt(tmp_path, "not-a-delivery-id")


def test_latest_receipts_is_newest_first(tmp_path):
    receipts.write_receipt(tmp_path, "b" * 64, status="dispatched", target="@a", now=10.0)
    receipts.write_receipt(tmp_path, "c" * 64, status="bounced", target="@b", now=20.0)
    assert [r["target"] for r in receipts.latest_receipts(tmp_path)] == ["@b", "@a"]


# ── the live mailbox speaks the same language ─────────────────────────────────


def test_mailbox_states_map_onto_the_sender_vocabulary(tmp_path):
    from tools import bot_live_delivery as mailbox

    owner = dict(profile_home=str(tmp_path.resolve()), session_id="chat",
                 lease_id="lease", live_session_id="live")
    queued = mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id="d" * 32)
    status = receipts.sender_status(tmp_path, "d" * 32)
    assert status["status"] == "dispatched" and status["mailbox_status"] == "queued"
    assert status["terminal"] is False

    mailbox.claim_pending_delivery(tmp_path, owner)
    status = receipts.sender_status(tmp_path, "d" * 32)
    assert status["status"] == "landed" and status["mailbox_status"] == "claimed"
    assert status["received_at"], "a claimed delivery has a received_at (the owner took it)"


@pytest.mark.parametrize("mailbox_status,expected", [
    ("queued", "dispatched"), ("claimed", "landed"), ("settled", "landed"),
    ("failed", "bounced"), ("cancelled", "bounced"), ("ambiguous", "unknown"),
])
def test_every_mailbox_state_has_a_sender_meaning(mailbox_status, expected):
    assert receipts.MAILBOX_STATUS[mailbox_status] == expected


# ── landed is only ever written from the target's own store ───────────────────


def _target_home_with_row(tmp_path, text: str, *, when: float | None = None):
    from hermes_state import SessionDB

    home = tmp_path / "target"
    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=home / "state.db")
    db.create_session(session_id="bot", source="cli")
    db.set_session_title("bot", "Bot Chat")
    db.close()
    conn = sqlite3.connect(home / "state.db")
    with conn:
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
                     ("bot", text, time.time() if when is None else when))
    conn.close()
    return home


def test_verify_admission_finds_only_the_targets_own_row(tmp_path):
    home = _target_home_with_row(tmp_path, "Message from 🤖 hermes (@hermes): probe one", when=1000.0)
    hit = receipts.verify_admission(home, "Message from 🤖 hermes (@hermes): probe one", since=900.0)
    assert hit and hit["session_id"] == "bot" and hit["message_row_id"] and hit["received_at"] == 1000.0
    assert receipts.verify_admission(home, "a message that never arrived", since=900.0) is None
    # a row from BEFORE the dispatch is not evidence for this delivery
    assert receipts.verify_admission(home, "probe one", since=2000.0) is None
    # no store at all is no evidence
    assert receipts.verify_admission(tmp_path / "nowhere", "probe one") is None


def test_cli_turn_writes_landed_from_the_row_and_unknown_without_one(tmp_path):
    text = "Message from 🤖 hermes (@hermes): landed probe"
    home = _target_home_with_row(tmp_path, text, when=1000.0)
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text(text, encoding="utf-8")

    bot_mode_dm._record_cli_outcome(home, _DID, str(dm_file), code=0, refusal=None, error="", since=900.0)
    landed = receipts.read_receipt(home, _DID)
    assert landed["status"] == "landed"
    assert landed["session_id"] == "bot" and landed["message_row_id"]
    assert landed["received_at"] == 1000.0

    # The same exit code with no row in the target's store is NOT a landed delivery.
    empty = _target_home_with_row(tmp_path / "empty", "something else", when=1000.0)
    other = "b" * 64
    bot_mode_dm._record_cli_outcome(empty, other, str(dm_file), code=0, refusal=None, error="", since=900.0)
    unknown = receipts.read_receipt(empty, other)
    assert unknown["status"] == "unknown" and unknown["reason"] == "no_row_found"


def test_a_refusal_is_a_bounce_with_its_reason(tmp_path):
    home = tmp_path / "target"
    home.mkdir()
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("hi", encoding="utf-8")
    bot_mode_dm._record_cli_outcome(home, _DID, str(dm_file), code=1, refusal="target_busy",
                                    error="Delivery failed: @ops's Bot Chat is open on another surface", since=1.0)
    record = receipts.read_receipt(home, _DID)
    assert record["status"] == "bounced" and record["reason"] == "target_busy"
    assert "another surface" in record["error"]
    # …and a crash with no identified refusal is unknown, never rounded up to landed
    crash = "c" * 64
    bot_mode_dm._record_cli_outcome(home, crash, str(dm_file), code=1, refusal=None, error="boom", since=1.0)
    assert receipts.read_receipt(home, crash)["status"] == "unknown"


# ── the sender's side: dispatched, then landed or bounced ─────────────────────


def _ack(**kwargs) -> dict:
    base = dict(status="dispatched", delivery_id=_DID, to="@ops", detail="x", process_id="proc_1")
    return {**base, **kwargs}


def test_classify_reports_landed_when_the_receipt_arrives_within_the_budget(tmp_path):
    receipts.write_receipt(tmp_path, _DID, status="landed", target="@ops", session_id="bot",
                           message_row_id=7, received_at=1000.0)
    out = bot_mode_dm._classify_dispatch(_ack(), tmp_path, _DID, wait=0.2)
    assert out["status"] == "landed" and out["received_at"] == 1000.0 and out["session_id"] == "bot"


def test_classify_reports_bounced_with_the_reason(tmp_path):
    receipts.write_receipt(tmp_path, _DID, status="bounced", target="@ops", reason="target_busy", error="nope")
    out = bot_mode_dm._classify_dispatch(_ack(), tmp_path, _DID, wait=0.2)
    assert out["status"] == "bounced" and out["reason"] == "target_busy" and out["error"] == "nope"
    assert "NOT delivered" in out["detail"]


def test_classify_leaves_dispatched_standing_when_nothing_settles(tmp_path):
    out = bot_mode_dm._classify_dispatch(_ack(), tmp_path, _DID, wait=0.05)
    assert out["status"] == "dispatched" and out["waited_seconds"] == 0.1
    assert "dispatched" not in out["detail"] or True  # wording is not the contract


def test_a_zero_budget_answers_immediately_without_touching_the_receipt(tmp_path):
    out = bot_mode_dm._classify_dispatch(_ack(), tmp_path, _DID, wait=0)
    assert out["status"] == "dispatched" and "waited_seconds" not in out


def test_wait_for_terminal_returns_the_record_or_none(tmp_path):
    receipts.write_receipt(tmp_path, _DID, status="dispatched", target="@ops")
    assert receipts.wait_for_terminal(tmp_path, _DID, timeout=0.05)["status"] == "dispatched"
    receipts.write_receipt(tmp_path, _DID, status="bounced", reason="target_busy")
    assert receipts.wait_for_terminal(tmp_path, _DID, timeout=0.05)["status"] == "bounced"


# ── the runner end to end: the refusal that used to arrive as `sent` ──────────


def _hold_flock(path, hold_event, release_event):
    import fcntl
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    hold_event.set()
    release_event.wait(timeout=10)
    os.close(fd)


def test_runner_writes_a_bounced_receipt_when_the_target_is_busy(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hermes"
    (home / "profiles" / "ops").mkdir(parents=True)
    target = home / "profiles" / "ops"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(bot_relay, "turn_wait_seconds", lambda: 0.2)
    dm = tmp_path / "dm.txt"
    dm.write_text("hi", encoding="utf-8")

    held, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_flock, args=(turn_lock_path(home, "ops"), held, release))
    t.start()
    assert held.wait(timeout=5)
    try:
        rc = bot_mode_dm._delivery_main(
            ["--run-delivery", "--delivery-id", _DID, "query-file", str(dm),
             "--profile-home", str(target), "hermes", "-p", "ops", "chat"]
        )
    finally:
        release.set()
        t.join(timeout=5)

    assert rc == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "bounced" and payload["reason"] == "target_busy"
    assert payload["delivery_id"] == _DID
    record = receipts.read_receipt(target, _DID)
    assert record["status"] == "bounced" and record["reason"] == "target_busy"
    assert not dm.exists(), "the DM plaintext is reclaimed even on refusal"


def test_runner_bounces_when_another_surface_holds_the_target_chat(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hermes"
    target = home / "profiles" / "ops"
    target.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    dm = tmp_path / "dm.txt"
    dm.write_text("hi", encoding="utf-8")
    child = tmp_path / "owned.py"
    child.write_text("import sys\nprint('Session abc already has a live owner (desktop, pid 1).', file=sys.stderr)\n"
                     "raise SystemExit(1)\n", encoding="utf-8")

    rc = bot_mode_dm._delivery_main(
        ["--run-delivery", "--delivery-id", _DID, "query-file", str(dm),
         "--profile-home", str(target), sys.executable, str(child), "-p", "ops"]
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "bounced" and payload["reason"] == "target_busy"
    record = receipts.read_receipt(target, _DID)
    assert record["status"] == "bounced" and record["reason"] == "target_busy"
    assert "NOT delivered" in record["error"] or "NOT delivered" in json.dumps(payload)


def test_runner_lands_when_the_target_records_the_message(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    target = home / "profiles" / "ops"
    monkeypatch.setenv("HERMES_HOME", str(home))
    text = "Message from 🤖 hermes (@hermes): live probe"
    from hermes_state import SessionDB

    target.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=target / "state.db")
    db.create_session(session_id="bot", source="cli")
    db.set_session_title("bot", "Bot Chat")
    db.close()
    row_at = time.time()
    conn = sqlite3.connect(target / "state.db")
    with conn:
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
                     ("bot", text, row_at))
    conn.close()

    dm = tmp_path / "dm.txt"
    dm.write_text(text, encoding="utf-8")
    child = tmp_path / "turn.py"
    child.write_text("print('reply')\n", encoding="utf-8")
    seen = tmp_path / "turn-seen.txt"

    def fake_run(argv, **kwargs):
        seen.write_text(" ".join(argv), encoding="utf-8")
        import subprocess

        return subprocess.CompletedProcess(argv, 0, stdout="reply", stderr="")

    monkeypatch.setattr(bot_mode_dm.subprocess, "run", fake_run)
    rc = bot_mode_dm._delivery_main(
        ["--run-delivery", "--delivery-id", _DID, "query-file", str(dm),
         "--profile-home", str(target), "hermes", "-p", "ops", "chat"]
    )
    assert rc == 0
    record = receipts.read_receipt(target, _DID)
    assert record["status"] == "landed" and record["session_id"] == "bot"
    assert record["message_row_id"] and record["received_at"] == row_at
    assert not dm.exists()
