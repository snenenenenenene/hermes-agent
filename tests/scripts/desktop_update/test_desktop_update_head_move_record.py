"""The desktop hand-off must never move the live checkout silently.

Live incident (2026-09-16): ``~/.hermes/hermes-agent`` was switched from
``local/profile-env-mirror`` to ``main`` (reflog ``checkout: moving from
local/profile-env-mirror to main``, then ``merge origin/main: Fast-forward``)
and the Desktop rebuilt itself from that tree minutes later. Every log was
silent about who did it — there was no record anywhere that HEAD had moved.

``hermes update`` is allowed to move HEAD (a parked branch under the default
``switch`` strategy is switched by design, and ``--switch-branch`` asks for
it), so the hand-off does not refuse. It RECORDS: ``head_state`` before the
update, ``head_state`` after, and on a difference a line in the hand-off log
plus a durable receipt under ``logs/update_receipts/head-moves.jsonl`` — with
an explicit violation marker when this home is configured for
``updates.parked_branch_strategy: update_in_place``.

These tests drive ``posix.sh --self-test-head-move`` (the script's real
``head_state`` / ``record_head_move_if_changed`` functions, no
re-implementation) against a real git repository.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
POSIX_SH = REPO_ROOT / "scripts" / "desktop-update" / "posix.sh"

requires_bash = pytest.mark.skipif(
    not os.path.exists("/bin/bash"), reason="posix.sh needs /bin/bash"
)

BEFORE = "local/profile-env-mirror 0fbac8fd0e3d58c587df910ce3a1ae60afa0eb99"
AFTER = "main 24fd22b94df040d843eb280ff197a4bcd99a6fc3"


def make_install_root(tmp_path: Path, *, config: str | None) -> Path:
    """A real git checkout at ``<tmp>/hermes-agent`` so ``head_state`` works.

    ``posix.sh`` derives HERMES_HOME from the install root's parent, so the
    config the violation check reads is ``<tmp>/config.yaml``.
    """
    root = tmp_path / "hermes-agent"
    root.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, env=env)
    (root / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "f.txt"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "c1"], check=True, env=env
    )
    if config is not None:
        (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
    return root


def run_self_test(root: Path, before: str, after: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HERMES_SELFTEST_HEAD_BEFORE": before,
        "HERMES_SELFTEST_HEAD_AFTER": after,
    }
    return subprocess.run(
        [
            "/bin/bash",
            str(POSIX_SH),
            "--self-test-head-move",
            "--install-root",
            str(root),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@requires_bash
def test_head_move_is_recorded_in_log_and_receipt(tmp_path):
    """A Desktop-driven HEAD move leaves two durable records and names the
    configured contract it violated."""
    root = make_install_root(
        tmp_path, config="updates:\n  parked_branch_strategy: update_in_place\n"
    )

    result = run_self_test(root, BEFORE, AFTER)

    assert result.returncode == 0, result.stderr
    assert "SELFTEST PASS" in result.stdout

    log_text = (tmp_path / "logs" / "desktop-update-handoff.log").read_text(
        encoding="utf-8"
    )
    assert "HEAD MOVED by the desktop update hand-off" in log_text
    assert BEFORE in log_text and AFTER in log_text
    assert "CONTRACT VIOLATED" in log_text
    assert "update_in_place" in log_text

    receipt = (tmp_path / "logs" / "update_receipts" / "head-moves.jsonl")
    receipt_text = receipt.read_text(encoding="utf-8")
    assert receipt_text.strip()
    assert "HEAD MOVED" in receipt_text
    assert BEFORE in receipt_text and AFTER in receipt_text


@requires_bash
def test_head_move_is_recorded_without_the_in_place_setting(tmp_path):
    """No setting configured: still recorded, just no violation marker."""
    root = make_install_root(tmp_path, config="updates: {}\n")

    result = run_self_test(root, BEFORE, AFTER)

    assert result.returncode == 0, result.stderr
    log_text = (tmp_path / "logs" / "desktop-update-handoff.log").read_text(
        encoding="utf-8"
    )
    assert "HEAD MOVED" in log_text
    assert "CONTRACT VIOLATED" not in log_text


@requires_bash
def test_unchanged_head_records_nothing(tmp_path):
    """The record is for moves. An update that leaves HEAD alone must not
    append noise — otherwise the line stops meaning anything."""
    root = make_install_root(
        tmp_path, config="updates:\n  parked_branch_strategy: update_in_place\n"
    )

    result = run_self_test(root, BEFORE, BEFORE)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "logs" / "update-head-moves.log").exists()
    assert not (tmp_path / "logs" / "update_receipts" / "head-moves.jsonl").exists()


@requires_bash
def test_self_test_requires_install_root_state(tmp_path):
    """No fabricated state: the self-test refuses to run without both sides."""
    root = make_install_root(tmp_path, config=None)

    result = run_self_test(root, "", "")

    assert result.returncode == 64
    assert "HERMES_SELFTEST_HEAD_BEFORE/AFTER are required" in result.stderr
