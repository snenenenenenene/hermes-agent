"""A claim never starts beside an open run whose worker is still alive."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def test_claim_refuses_live_open_run_then_claims_when_worker_is_dead(conn, monkeypatch):
    tid = kb.create_task(conn, title="one worker", assignee="coder")
    assert kb.claim_task(conn, tid, claimer="first") is not None
    run_id = kb._current_run_id(conn, tid)
    worker_pid = 424242
    conn.execute(
        "UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
        (worker_pid, 123.0, run_id),
    )
    conn.execute(
        "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
        "worker_pid = NULL, worker_started_at = NULL, current_run_id = NULL WHERE id = ?",
        (tid,),
    )
    conn.commit()

    alive = True
    monkeypatch.setattr(kb, "_worker_alive", lambda pid, started_at: alive)
    assert kb.claim_task(conn, tid, claimer="second") is None
    rejected = kb.list_events(conn, tid)[-1]
    assert rejected.kind == "claim_rejected"
    assert rejected.payload == {
        "reason": "live_run_row",
        "run_id": run_id,
        "worker_pid": worker_pid,
    }
    assert conn.execute(
        "SELECT ended_at FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()["ended_at"] is None

    alive = False
    claimed = kb.claim_task(conn, tid, claimer="second")
    assert claimed is not None and claimed.status == "running"
    prior = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert (prior["status"], prior["outcome"]) == ("reclaimed", "reclaimed")
    assert prior["ended_at"] is not None


def test_reclaim_dangling_run_closes_every_open_row(conn):
    tid = kb.create_task(conn, title="two orphans", assignee="coder")
    now = 123456
    conn.executemany(
        "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', ?)",
        ((tid, now - 2), (tid, now - 1)),
    )
    conn.commit()

    kb._reclaim_dangling_run(
        conn, tid, statuses=("ready",), now=now, note="test recovery",
    )
    rows = conn.execute(
        "SELECT status, outcome, ended_at, summary FROM task_runs "
        "WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    assert len(rows) == 2
    assert all(
        (row["status"], row["outcome"], row["ended_at"], row["summary"])
        == ("reclaimed", "reclaimed", now, "test recovery")
        for row in rows
    )


def test_orphaned_open_run_with_pr_comment_is_claimable(conn):
    tid = kb.create_task(conn, title="resume PR", assignee="coder")
    assert kb.claim_task(conn, tid, claimer="first") is not None
    run_id = kb._current_run_id(conn, tid)
    kb.add_comment(
        conn, tid, author="coder",
        body="Opened https://github.com/example/project/pull/123",
    )
    conn.execute(
        "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
        "worker_pid = NULL, worker_started_at = NULL, current_run_id = NULL WHERE id = ?",
        (tid,),
    )
    conn.commit()

    task = kb.get_task(conn, tid)
    open_run = kb.get_run(conn, run_id)
    assert task.status == "ready" and task.current_run_id is None
    assert open_run.status == "running" and open_run.ended_at is None
    assert open_run.claim_expires > int(time.time())

    assert kbd.check_respawn_guard(conn, tid) is None
    claimed = kb.claim_task(conn, tid, claimer="second")
    assert claimed is not None and claimed.status == "running"
    assert kb.get_run(conn, run_id).outcome == "reclaimed"
