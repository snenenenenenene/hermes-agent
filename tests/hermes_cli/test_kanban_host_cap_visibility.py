"""Host-cap visibility for the kanban dispatcher.

``kanban.max_in_progress`` is a HOST-wide cap: a tick that sees the cap already
met (running workers on this board plus every other board) returns with no
spawn and — before ``DispatchResult.host_cap_held`` — looked identical to an
idle board ("Spawned: 0, every skip bucket empty"). The new flag plus
``describe_suppression``'s ``host_cap=1`` output make that hold visible so a
starving board is diagnosable instead of silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _running_task(conn) -> str:
    """Create and claim a task so it is a genuine ``running`` worker row.

    ``create_task``'s ``initial_status`` only forces ``blocked``; a claimed task
    carries a live ``claim_lock``/``claim_expires`` so the reclaim phase does not
    requeue it as an orphan before the budget is read.
    """
    task_id = kb.create_task(conn, title="t", assignee="alice")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.status == "running"
    return task_id


def test_host_cap_sets_flag_and_names_in_suppression(conn):
    """A running worker with max_in_progress=1 blocks the tick on the host cap,
    sets ``host_cap_held`` and is reported as ``host_cap=1``."""
    _running_task(conn)
    result = kbd.dispatch_once(conn, max_in_progress=1, spawn_fn=lambda *a, **k: 4242)
    assert result.host_cap_held is True
    assert result.spawned == []
    assert "host_cap=1" in kbd.describe_suppression([result])


def test_max_spawn_suppression_does_not_set_host_cap(conn):
    """The per-board ``max_spawn`` cap is a different suppression path: the tick
    still spawns nothing, but ``host_cap_held`` must stay False."""
    _running_task(conn)
    result = kbd.dispatch_once(conn, max_spawn=1, spawn_fn=lambda *a, **k: 4242)
    assert result.host_cap_held is False
    assert result.spawned == []
    assert "host_cap" not in kbd.describe_suppression([result])


def test_idle_tick_reports_no_host_cap(conn):
    """An uncapped tick with nothing running reports an empty suppression line
    and does not set the flag."""
    result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    assert result.host_cap_held is False
    assert kbd.describe_suppression([result]) == ""


def test_describe_suppression_counts_multiple_host_cap_holds():
    """``host_cap`` is counted per result like the other tick-level holds."""
    results = [
        kb.DispatchResult(host_cap_held=True),
        kb.DispatchResult(host_cap_held=True),
    ]
    assert "host_cap=2" in kbd.describe_suppression(results)
