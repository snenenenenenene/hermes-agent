"""Regression tests for _apply_profile_override's HERMES_HOME guard (issue #22502).

When HERMES_HOME is set to the hermes root (e.g. systemd hardcodes
HERMES_HOME=/root/.hermes), _apply_profile_override must still read
active_profile and update HERMES_HOME to the profile directory.

When HERMES_HOME is already a profile directory (.../profiles/<name>),
_apply_profile_override must trust it and return without re-reading
active_profile (child-process inheritance contract).

HERMES_PROFILE mirrors the effective home in every one of those paths: the
process that runs on a profile's home must name that profile, because
tools/kanban_tools.py, hermes_cli/kanban.py and the cron guards read the env var
as the audit author (t_1b9a5cce).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple


class _ProfileRun(NamedTuple):
    """Post-call process identity: where the home resolved, which name it mirrors."""

    home: str | None
    profile: str | None


def _run_apply_profile_override(
    tmp_path, monkeypatch, *, hermes_home: str | None, active_profile: str | None,
    argv: list[str] | None = None, extra_env: dict[str, str] | None = None,
    hermes_profile: str | None = None,
) -> _ProfileRun:
    """Run _apply_profile_override in isolation.

    Returns the post-call (HERMES_HOME, HERMES_PROFILE) pair; either entry is
    ``None`` when the call left it unset. ``hermes_profile`` seeds the *inherited*
    value: the var is always scrubbed first, so a test that passes nothing starts
    from "unset" rather than from the running session's own HERMES_PROFILE — that
    stale inheritance is the shape half of this contract is about.
    """
    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir(parents=True, exist_ok=True)

    if active_profile is not None:
        (hermes_root / "active_profile").write_text(active_profile)

    if active_profile and active_profile != "default":
        (hermes_root / "profiles" / active_profile).mkdir(parents=True, exist_ok=True)
        (hermes_root / "profiles" / active_profile / "config.yaml").write_text("{}\n")  # identity marker

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if hermes_home is not None:
        monkeypatch.setenv("HERMES_HOME", hermes_home)
    else:
        monkeypatch.delenv("HERMES_HOME", raising=False)

    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    if hermes_profile is not None:
        monkeypatch.setenv("HERMES_PROFILE", hermes_profile)

    monkeypatch.setattr(sys, "argv", argv or ["hermes", "gateway", "start"])

    # Scrub supervisor markers the host environment may carry (systemd-run
    # CI runners export INVOCATION_ID) so each test controls them explicitly.
    for var in (
        "HERMES_SUPERVISED_CHILD",
        "HERMES_S6_SUPERVISED_CHILD",
        "INVOCATION_ID",
        "HERMES_GATEWAY_EXTERNAL_SUPERVISOR",
    ):
        monkeypatch.delenv(var, raising=False)

    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)

    from hermes_cli.main import _apply_profile_override
    _apply_profile_override()

    return _ProfileRun(
        os.environ.get("HERMES_HOME"), os.environ.get("HERMES_PROFILE")
    )


class TestApplyProfileOverrideHermesHomeGuard:
    """Regression guard for issue #22502.

    Verifies that HERMES_HOME pointing to the hermes root does NOT suppress
    the active_profile check, while HERMES_HOME already pointing to a
    profile directory IS trusted as-is.
    """

    def test_hermes_home_at_root_with_active_profile_is_redirected(
        self, tmp_path, monkeypatch
    ):
        """HERMES_HOME=/root/.hermes + active_profile=coder must redirect
        HERMES_HOME to .../profiles/coder.

        Bug scenario from #22502: systemd sets HERMES_HOME to the hermes root
        and the user switches to a profile via `hermes profile use`.
        Before the fix, the guard returned early and active_profile was ignored.
        """
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)

        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="coder",
        )

        assert env.home is not None, "HERMES_HOME must be set after profile redirect"
        assert "profiles" in env.home, (
            f"Expected HERMES_HOME to point into profiles/ dir, got: {env.home!r}"
        )
        assert env.home.endswith("coder"), (
            f"Expected HERMES_HOME to end with 'coder', got: {env.home!r}"
        )
        assert env.profile == "coder", (
            f"Expected HERMES_PROFILE to follow the redirected home, got: {env.profile!r}"
        )


    def test_sudo_explicit_profile_resolves_invoking_users_profile(self, tmp_path, monkeypatch):
        """sudo elias ... should resolve `-p elias` under SUDO_USER, not root."""
        root_home = tmp_path / "root"
        user_home = tmp_path / "home" / "hermes"
        profile_dir = user_home / ".hermes" / "profiles" / "elias"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "config.yaml").write_text("{}\n")  # identity marker: a bare dir does not resolve
        (root_home / ".hermes").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: root_home)
        monkeypatch.setenv("SUDO_USER", "hermes")
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(sys, "argv", ["hermes", "-p", "elias", "gateway", "install", "--system"])

        import pwd

        monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir=str(user_home)))

        from hermes_cli.main import _apply_profile_override, _resolve_sudo_user_profile_env
        _apply_profile_override()

        assert os.environ.get("HERMES_HOME") == str(profile_dir)
        assert os.environ.get("HERMES_PROFILE") == "elias"
        assert sys.argv == ["hermes", "gateway", "install", "--system"]
        # Same identity gate as ``-p`` without sudo: a marker-less shell is not a profile.
        (user_home / ".hermes" / "profiles" / "ghost" / "cron").mkdir(parents=True)
        assert _resolve_sudo_user_profile_env("ghost") is None




class TestSupervisedChildIgnoresStickyProfile:
    """The reserved default gateway s6 slot must not follow active_profile.

    Inside the Docker s6 image the ``gateway-default`` service slot runs a
    bare ``hermes gateway run`` (no ``-p``) to mean "the root HERMES_HOME
    profile". The run-script exports ``HERMES_S6_SUPERVISED_CHILD=1``.
    Without a guard, ``_apply_profile_override`` would read the sticky
    ``active_profile`` file (set by e.g. the dashboard profile switcher) and
    redirect the reserved default gateway into that profile — producing a
    duplicate gateway for the active profile and no real default gateway.
    """


    def test_non_supervised_run_still_follows_active_profile(
        self, tmp_path, monkeypatch
    ):
        """Without the sentinel, a normal `hermes gateway run` still honors
        active_profile — the guard is scoped strictly to supervised children."""
        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=None,
            active_profile="briefer",
            argv=["hermes", "gateway", "run"],
        )

        assert env.home is not None
        assert env.home.endswith("briefer")
        assert env.profile == "briefer"

    def test_supervised_named_profile_flag_still_wins(self, tmp_path, monkeypatch):
        """A supervised named-profile slot passes ``-p <name>`` explicitly;
        that must still resolve (the sentinel guard only skips the sticky
        active_profile fallback, never an explicit flag)."""
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)
        (hermes_root / "active_profile").write_text("briefer")
        for name in ("briefer", "coder"):
            (hermes_root / "profiles" / name).mkdir(parents=True, exist_ok=True)
            (hermes_root / "profiles" / name / "config.yaml").write_text("{}\n")  # identity marker

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        monkeypatch.setenv("HERMES_S6_SUPERVISED_CHILD", "1")
        monkeypatch.setattr(sys, "argv", ["hermes", "-p", "coder", "gateway", "run"])

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        home = os.environ.get("HERMES_HOME")
        assert home is not None
        assert home.endswith("coder")
        assert os.environ.get("HERMES_PROFILE") == "coder"



class TestGeneralizedSupervisorMarkers:
    """Regression tests for issue #74872.

    A systemd/launchd/Scheduled-Task supervised gateway launch pins its
    profile identity via the unit's HERMES_HOME (root home for the default
    profile). It must NEVER follow the sticky ``active_profile`` file —
    otherwise the default-profile gateway silently assumes another profile's
    identity (logs + Telegram bot token) and double-polls that profile's
    token. Markers: HERMES_SUPERVISED_CHILD (generalized, exported by
    generated units), INVOCATION_ID (systemd, gateway commands only), and
    HERMES_GATEWAY_EXTERNAL_SUPERVISOR (explicit opt-in).
    """

    def _root_home(self, tmp_path):
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)
        return hermes_root

    def test_supervised_child_marker_skips_active_profile(
        self, tmp_path, monkeypatch
    ):
        """HERMES_SUPERVISED_CHILD=1 + root HERMES_HOME must keep the
        default profile's home even when active_profile names another
        profile (the #74872 identity-assumption vector)."""
        hermes_root = self._root_home(tmp_path)
        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="telegram_nick",
            argv=["hermes", "gateway", "run"],
            extra_env={"HERMES_SUPERVISED_CHILD": "1"},
        )
        assert env.home == str(hermes_root), (
            f"supervised default gateway was redirected to {env.home!r}"
        )
        assert env.profile == "default", (
            f"supervised default slot must name the default profile, got {env.profile!r}"
        )

    def test_systemd_invocation_id_skips_active_profile_for_gateway(
        self, tmp_path, monkeypatch
    ):
        """INVOCATION_ID (systemd service child) must suppress the sticky
        redirect for gateway commands — covers units installed before the
        HERMES_SUPERVISED_CHILD marker existed."""
        hermes_root = self._root_home(tmp_path)
        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="telegram_nick",
            argv=["hermes", "gateway", "run"],
            extra_env={"INVOCATION_ID": "deadbeef" * 4},
        )
        assert env.home == str(hermes_root)
        assert env.profile == "default"

    def test_invocation_id_does_not_affect_non_gateway_commands(
        self, tmp_path, monkeypatch
    ):
        """INVOCATION_ID leaks into every descendant of a systemd-launched
        process (CI runners, user services). Non-gateway commands must keep
        honoring the sticky active_profile."""
        hermes_root = self._root_home(tmp_path)
        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="coder",
            argv=["hermes", "chat"],
            extra_env={"INVOCATION_ID": "deadbeef" * 4},
        )
        assert env.home is not None
        assert env.home.endswith("coder")
        assert env.profile == "coder"

    def test_external_supervisor_marker_skips_active_profile(
        self, tmp_path, monkeypatch
    ):
        hermes_root = self._root_home(tmp_path)
        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="telegram_nick",
            argv=["hermes", "gateway", "run"],
            extra_env={"HERMES_GATEWAY_EXTERNAL_SUPERVISOR": "1"},
        )
        assert env.home == str(hermes_root)
        assert env.profile == "default"

    def test_desktop_ssh_serve_child_skips_active_profile(self, tmp_path, monkeypatch):
        """A Desktop-owned `serve --ssh-session-token-file` child names its profile explicitly
        (or none for the root home); the remote host's sticky active_profile must not re-home
        it, or Settings read one profile's config.yaml while the user edits another."""
        hermes_root = self._root_home(tmp_path)
        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="telegram_nick",
            argv=["hermes", "serve", "--isolated", "--host", "127.0.0.1", "--port", "0",
                  "--ssh-session-token-file", "/tmp/x/y.token"],
        )
        assert env.home == str(hermes_root)
        assert env.profile == "default"

    def test_generated_systemd_unit_exports_supervised_marker(
        self, tmp_path, monkeypatch
    ):
        """The generated systemd unit must carry the marker so fresh installs
        are protected without relying on the INVOCATION_ID heuristic."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        from hermes_cli.gateway import generate_systemd_unit

        unit = generate_systemd_unit()
        assert 'Environment="HERMES_SUPERVISED_CHILD=1"' in unit

    def test_generated_launchd_plist_exports_supervised_marker(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        from hermes_cli.gateway import generate_launchd_plist

        plist = generate_launchd_plist()
        assert "<key>HERMES_SUPERVISED_CHILD</key>" in plist


class TestProfileEnvMirrorsEffectiveHome:
    """``HERMES_PROFILE`` must name the profile ``HERMES_HOME`` points at (t_1b9a5cce).

    The env var is the audit author for kanban comments, attachments,
    ``created_by`` and the notifier profile (``tools/kanban_tools.py`` ~497/741/
    896/920, ``hermes_cli/kanban.py:203``, ``hermes_cli/kanban_specify.py:122``),
    and the profile key for secret scope + cron lifecycle guards. A CLI child that
    re-homed itself onto another profile while keeping the parent's name therefore
    wrote another agent's work under its own name — the divergence the roster's
    one-name rule exists to prevent.
    """

    def _profile_home(self, tmp_path, name: str) -> Path:
        path = tmp_path / ".hermes" / "profiles" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_flag_overrides_an_inherited_mismatched_pair(self, tmp_path, monkeypatch):
        """The defect: a `--profile erin` child spawned from a darryl session.

        Inherited HERMES_HOME=profiles/darryl + HERMES_PROFILE=darryl, argv
        ``hermes --profile erin ...``. Before the fix the home moved to erin's
        while the name stayed darryl; both must now say erin.
        """
        erin_home = self._profile_home(tmp_path, "erin")

        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(tmp_path / ".hermes" / "profiles" / "darryl"),
            active_profile=None,
            argv=["hermes", "--profile", "erin", "--help"],
            hermes_profile="darryl",
        )

        assert env.home == str(erin_home), (
            f"HERMES_HOME must follow the flag, got {env.home!r}"
        )
        assert env.profile == "erin", (
            f"HERMES_PROFILE must follow the home, got {env.profile!r} (was 'darryl')"
        )

    def test_profile_shaped_home_with_profile_unset_names_that_profile(
        self, tmp_path, monkeypatch
    ):
        """A parent-inherited profile home with no flag and no name must be named
        from the home itself — the sticky active_profile file is never consulted
        on this path (child-process inheritance contract)."""
        elias_home = self._profile_home(tmp_path, "elias")

        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(elias_home),
            active_profile="coder",
            hermes_profile=None,
        )

        assert env.home == str(elias_home)
        assert env.profile == "elias"

    def test_profile_shaped_home_beats_a_disagreeing_inherited_profile(
        self, tmp_path, monkeypatch
    ):
        """Second shape of the bug: the env pair is already inconsistent when the
        process starts (worker spawned with HERMES_HOME pinned to the profile but
        the dispatching gateway's HERMES_PROFILE inherited). The home wins."""
        toby_home = self._profile_home(tmp_path, "toby")

        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(toby_home),
            active_profile=None,
            argv=["hermes", "chat", "-q", "hi"],
            hermes_profile="darryl",
        )

        assert env.home == str(toby_home)
        assert env.profile == "toby", (
            f"a disagreeing inherited name must be corrected, got {env.profile!r}"
        )

    def test_root_home_fills_an_unset_profile_with_default(
        self, tmp_path, monkeypatch
    ):
        """Root home, nothing to resolve: the identity is still filled in, so a
        default-profile session or supervised default slot never reports an empty
        author."""
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)

        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile=None,
            argv=["hermes", "chat"],
            hermes_profile=None,
        )

        assert env.home == str(hermes_root)
        assert env.profile == "default"

    def test_explicit_default_flag_resolves_to_default(self, tmp_path, monkeypatch):
        """``-p default`` re-homes onto the root and must be named ``default``,
        not the caller's inherited profile."""
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)

        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root / "profiles" / "darryl"),
            active_profile=None,
            argv=["hermes", "-p", "default", "chat"],
            hermes_profile="darryl",
        )

        assert env.home == str(hermes_root)
        assert env.profile == "default"

    def test_root_home_leaves_an_operator_set_profile_alone(
        self, tmp_path, monkeypatch
    ):
        """Deliberate boundary of the fill-only rule: with a root home and NO
        explicit flag, an already-set HERMES_PROFILE is not reinterpreted.

        Only the flag / active_profile / profile-shaped-home paths carry the
        authority to rename the process; a bare ``HERMES_PROFILE=label hermes``
        with no HERMES_HOME is an operator decision this function must not
        silently overwrite (#22502's inverse)."""
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)

        env = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile=None,
            argv=["hermes", "chat"],
            hermes_profile="operator-label",
        )

        assert env.home == str(hermes_root)
        assert env.profile == "operator-label"
