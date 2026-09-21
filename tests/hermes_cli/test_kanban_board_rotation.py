"""Board-order rotation for the embedded kanban dispatcher.

The dispatcher ticks every active board in ``list_boards`` order. Because
``kanban.max_in_progress`` is a HOST-wide cap, the first board in that fixed
order always consumes the whole budget while later boards starve. ``tick_once``
now rotates the slug list by its per-instance tick counter so each successive
tick starts with a different board (round-robin).
"""

from __future__ import annotations

from gateway.kanban_watchers_dispatcher import (
    _DispatcherSettings,
    _KanbanDispatcher,
    _rotate_board_slugs,
)


def test_rotate_board_slugs_round_robin():
    slugs = ["default", "alpha", "beta"]
    assert _rotate_board_slugs(slugs, 0) == ["default", "alpha", "beta"]
    assert _rotate_board_slugs(slugs, 1) == ["alpha", "beta", "default"]
    assert _rotate_board_slugs(slugs, 2) == ["beta", "default", "alpha"]
    # Wraps back to the original order at a full cycle.
    assert _rotate_board_slugs(slugs, 3) == ["default", "alpha", "beta"]


def test_rotate_board_slugs_handles_zero_and_one():
    assert _rotate_board_slugs([], 5) == []
    assert _rotate_board_slugs(["only"], 7) == ["only"]


def test_rotate_board_slugs_does_not_mutate_input():
    slugs = ["default", "alpha", "beta"]
    _rotate_board_slugs(slugs, 1)
    assert slugs == ["default", "alpha", "beta"]


class _FakeKB:
    DEFAULT_BOARD = "default"

    def __init__(self, slugs):
        self._slugs = slugs

    def list_boards(self, include_archived=False):
        return [{"slug": s} for s in self._slugs]


class _RecordingDispatcher(_KanbanDispatcher):
    """Overrides the per-board DB work to just record the tick's slug order."""

    def __init__(self, slugs):
        super().__init__(
            _FakeKB(slugs),
            _DispatcherSettings(
                interval=60.0,
                max_spawn=None,
                max_in_progress=None,
                failure_limit=1,
                stale_timeout_seconds=0,
                reconcile_orphans=True,
                default_assignee=None,
                max_in_progress_per_profile=None,
            ),
        )
        self.orders: list[list[str]] = []
        self._current: list[str] = []

    def tick_once_for_board(self, slug):
        self._current.append(slug)
        return slug

    def tick_once(self):
        self._current = []
        result = super().tick_once()
        self.orders.append(list(self._current))
        return result


def test_tick_once_rotates_start_board_each_tick():
    """With three boards, consecutive ticks start with a different board and no
    board is permanently first."""
    dispatcher = _RecordingDispatcher(["default", "alpha", "beta"])
    for _ in range(3):
        dispatcher.tick_once()

    starts = [order[0] for order in dispatcher.orders]
    # Round-robin: each tick begins one slot further along the natural order.
    assert starts == ["alpha", "beta", "default"]
    # Every board gets a turn first — none is permanently first.
    assert set(starts) == {"default", "alpha", "beta"}
