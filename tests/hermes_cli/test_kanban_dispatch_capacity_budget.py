from __future__ import annotations

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
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize("budget", [90, 150, 240])
def test_worker_argv_keeps_budget_in_chat_options_and_task_as_query(
    kanban_home, monkeypatch, budget,
):
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="big card",
            body=f"Do the work. call budget: {budget}.",
            assignee="default",
        )
        task = kb.get_task(conn, tid)
        assert task is not None
    argv = kbd._worker_argv(task, "default", str(kanban_home))
    chat_index = argv.index("chat")
    budget_index = argv.index("--max-turns")
    assert "--max_turns" not in argv
    assert str(budget) not in argv[:chat_index]
    assert chat_index < budget_index
    assert argv[budget_index + 1] == str(budget)

    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()
    assert argv[1:3] == ["-p", "default"]
    args = parser.parse_args(argv[3:])
    assert args.command == "chat"
    assert args.max_turns == budget
    assert args.query == f"work kanban task {tid}"


def test_worker_argv_derives_larger_budget_from_card_size(kanban_home):
    body = "\n".join(f"- step {i}" for i in range(12))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="large card", body=body, assignee="default")
        task = kb.get_task(conn, tid)
        assert task is not None
    argv = kbd._worker_argv(task, "default", str(kanban_home))
    assert argv[argv.index("--max-turns") + 1] == str(kbd.KANBAN_WORKER_LARGE_TURNS)


def test_worker_argv_ignores_descriptive_max_turns_mentions(kanban_home):
    body = "agent.max_turns, 60 in every profile\n" + "\n".join(f"- step {i}" for i in range(12))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="large descriptive card", body=body, assignee="default")
        task = kb.get_task(conn, tid)
        assert task is not None
    argv = kbd._worker_argv(task, "default", str(kanban_home))
    assert argv[argv.index("--max-turns") + 1] == str(kbd.KANBAN_WORKER_LARGE_TURNS)


def test_capacity_exhaustion_requeues_once_without_counting_failure(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="needs more room", assignee="default")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        split = kbd._record_task_capacity_exhausted(
            conn,
            tid,
            error="Iteration budget exhausted (90/90) — task could not complete within the allowed iterations",
            event_payload_extra={"budget_used": 90, "budget_max": 90},
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        runs = conn.execute("select outcome from task_runs where task_id=?", (tid,)).fetchall()
        events = [e.kind for e in kb.list_events(conn, tid)]
    assert split is False
    assert task.status == "ready"
    assert task.consecutive_failures == 0
    assert [r["outcome"] for r in runs] == ["capacity"]
    assert "capacity_requeued" in events


def test_second_capacity_exhaustion_parks_as_split_signal(kanban_home):
    error = "Iteration budget exhausted (90/90) — task could not complete within the allowed iterations"
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="too large", assignee="default")
        assert kb.claim_task(conn, tid) is not None
        kbd._record_task_capacity_exhausted(conn, tid, error=error)
        assert kb.claim_task(conn, tid) is not None
        split = kbd._record_task_capacity_exhausted(conn, tid, error=error)
        task = kb.get_task(conn, tid)
        assert task is not None
        runs = conn.execute("select outcome from task_runs where task_id=? order by id", (tid,)).fetchall()
        comments = [c.body for c in kb.list_comments(conn, tid)]
    assert split is True
    assert task.status == "blocked"
    assert task.block_kind == "transient"
    assert task.consecutive_failures == 0
    assert [r["outcome"] for r in runs] == ["capacity", "capacity_split"]
    assert any("split this card smaller" in c for c in comments)
