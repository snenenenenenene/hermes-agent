"""Regression: a fresh claim must never inherit the PREVIOUS run's heartbeat.

Incident (task t_4568452e): a claim opened at T0 carried a
``tasks.last_heartbeat_at`` from an earlier run hours before T0. On the first
reclaim pass after the TTL expired, ``release_stale_claims`` read that inherited
value, computed ``heartbeat_stale=True`` for a live and healthy worker, SIGTERM'd
it, and booked the reclaim as a task failure — with ``failure_limit=1`` the card
then ``gave_up`` on ``stale_lock=<lock>`` and blocked every retry.

Root cause: the claim/release UPDATEs cleared ``worker_pid`` /
``worker_started_at`` but not ``last_heartbeat_at``, so heartbeat state crossed a
run boundary. A new run must start with no heartbeat history.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _spawn_sleep(seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen(["sleep", str(seconds)])


def _row(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()


def test_claim_clears_inherited_heartbeat_and_spares_a_live_worker(conn):
    """A stale heartbeat left by a prior run must not make the reclaim backstop
    kill a worker whose claim was just opened."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="inherited heartbeat", assignee="w")

    # A previous run left a heartbeat 8h old on the row (exactly the state the
    # incident started from).
    conn.execute(
        "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
        (int(time.time()) - 8 * 3600, tid),
    )
    conn.commit()

    kb.claim_task(conn, tid, claimer=f"{host}:K")
    claimed = _row(conn, tid)
    assert claimed["last_heartbeat_at"] is None, (
        "a fresh claim must not inherit last_heartbeat_at from a previous run"
    )

    # A live, healthy worker owns the claim; give it a stale-looking expiry.
    proc = _spawn_sleep()
    try:
        kbd._set_worker_pid(conn, tid, proc.pid)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 1, tid)
        )
        conn.commit()

        signalled: list[int] = []

        def signal_fn(pid, sig):
            signalled.append(sig)

        reclaimed = kb.release_stale_claims(conn, signal_fn=signal_fn)

        assert reclaimed == 0, "a live worker with a fresh claim must be extended, not reclaimed"
        assert signal.SIGTERM not in signalled, "a live worker must not be signalled"
        after = _row(conn, tid)
        assert after["status"] == "running"
        assert after["claim_lock"] == f"{host}:K"
        assert after["worker_pid"] == proc.pid
    finally:
        proc.kill()
        proc.wait()


def test_killed_worker_leaves_no_stale_state_and_next_run_proceeds(
    conn, all_assignees_spawnable
):
    """Kill a real spawned worker mid-run: the crash sweep must clear heartbeat
    state and the next dispatch tick must open a new run."""
    tid = kb.create_task(conn, title="kill mid-run", assignee="w")

    procs: list[subprocess.Popen] = []

    def spawn_tracked(task, workspace, board=None):
        p = _spawn_sleep(120)
        procs.append(p)
        return p.pid

    kbd.dispatch_once(conn, spawn_fn=spawn_tracked, max_spawn=1)
    first = _row(conn, tid)
    assert first["status"] == "running", "task should have been claimed and spawned"
    first_run = first["current_run_id"]
    first_pid = first["worker_pid"]

    procs[0].kill()
    procs[0].wait()

    kbd.detect_crashed_workers(conn)
    released = _row(conn, tid)
    assert released["claim_lock"] is None, "a dead worker must not keep its claim"
    assert released["worker_pid"] is None
    assert released["last_heartbeat_at"] is None, (
        "the crash sweep must not leave heartbeat state for the next run to inherit"
    )

    kbd.dispatch_once(conn, spawn_fn=spawn_tracked, max_spawn=1)
    second = _row(conn, tid)
    assert second["status"] == "running", "the next run must proceed"
    assert second["current_run_id"] != first_run, "a new run must have been opened"
    assert second["worker_pid"] not in (None, first_pid)
    assert second["last_heartbeat_at"] is None
    try:
        os.kill(int(second["worker_pid"]), 0)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
