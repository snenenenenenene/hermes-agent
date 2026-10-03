"""Regression for the stuck targets reported by the 2026-10-02 fleet lookback."""

from __future__ import annotations

import time
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def test_repeated_target_approach_is_parked_with_one_corrective_card(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")
    kb.init_db()

    error = (
        "pid 12613 not alive Worker's last output: \"hermes: '150' is not a `hermes` "
        "command. Run `hermes --help` to see all commands.\""
    )
    now = int(time.time())
    spawned = []

    with kbc.connect() as conn:
        # Realistic form of scritchreddit2026: a fresh dated card kept using
        # the same broad approach after the previous dated cards crashed.
        for offset, day in enumerate(("27", "28", "29"), start=1):
            old_id = kb.create_task(
                conn,
                title=f"Scritch Reddit 2026-09-{day}: answer every thread Scritch genuinely helps",
                assignee="default",
                initial_status="blocked",
            )
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome, error) "
                "VALUES (?, 'default', 'crashed', ?, ?, 'crashed', ?)",
                (old_id, now - offset * 60, now - offset * 60 + 1, error),
            )

        current_id = kb.create_task(
            conn,
            title="Scritch Reddit 2026-10-03: answer every thread Scritch genuinely helps",
            assignee="default",
        )

        def spawn(*args, **kwargs):
            spawned.append((args, kwargs))
            return 12345

        first = kbd.dispatch_once(
            conn, spawn_fn=spawn, failure_limit=3, reconcile_orphans=False,
        )
        second = kbd.dispatch_once(
            conn, spawn_fn=spawn, failure_limit=3, reconcile_orphans=False,
        )

        current = kb.get_task(conn, current_id)
        corrective = conn.execute(
            "SELECT id, title, body, status FROM tasks "
            "WHERE idempotency_key = 'stuck-target:scritchreddit2026'"
        ).fetchall()
        event_kinds = [event.kind for event in kb.list_events(conn, current_id)]

    assert spawned == []
    assert current.status == "blocked"
    assert current_id in first.auto_blocked
    assert second.auto_blocked == []
    assert "claimed" not in event_kinds
    assert len(corrective) == 1
    assert corrective[0]["status"] == "blocked"
    assert corrective[0]["title"] == "Correct stuck target: scritchreddit2026"
    body = corrective[0]["body"]
    assert "Target: scritchreddit2026" in body
    assert "Failing approach: Scritch Reddit 2026-10-03" in body
    assert "Last output:" in body and "'150' is not a `hermes` command" in body
    assert (
        f'Exact next experiment: Run `hermes chat --max-turns 150 -q "work kanban task {current_id}"` once'
        in body
    )
