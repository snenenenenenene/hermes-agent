from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def test_pinned_provider_outage_holds_ready_card(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "state").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")
    (home / "state" / "provider-policy.json").write_text(json.dumps({
        "outages": {
            "openai-codex": {"reason": "completion_quota_wall"},
            "smoke-codex-223957": {"reason": "polluted_probe_key"},
        },
    }), encoding="utf-8")
    (home / "state" / "fallback_chain.json").write_text(json.dumps({
        "rungs": [{
            "provider": "openai-codex", "model": "gpt-5.5", "verdict": "native-sdk",
        }],
    }), encoding="utf-8")
    kb.init_db()

    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn, title="must wait", assignee="default",
            model_override="gpt-5.5", provider_override="openai-codex",
        )
        def fail_spawn(*_args, **_kwargs):
            raise AssertionError("unhealthy provider must not spawn")

        result = kbd.dispatch_once(conn, spawn_fn=fail_spawn)
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert task is not None and task.status == "ready" and task.claim_lock is None
    assert result.spawned == []
    reason = dict(result.respawn_guarded)[task_id]
    assert "pinned provider openai-codex" in reason
    assert "policy outage" in reason
    assert any(event.kind == "respawn_guarded" and event.payload["reason"] == reason for event in events)
