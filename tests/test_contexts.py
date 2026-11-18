"""Deterministic lease boundaries supplement the real HTTP acceptance tests."""

import pytest

from hermes2mcp.contexts import NativeContexts


@pytest.mark.parametrize("limit", [0, -1, 1.5, float("inf"), float("nan"), True, None, "1"])
def test_native_contexts_reject_invalid_capacity_before_starting_a_reaper(limit):
    with pytest.raises(ValueError, match="positive integer"):
        NativeContexts(lambda session: None, limit=limit)


@pytest.mark.parametrize(
    "timeout",
    [0, -1, float("inf"), float("-inf"), float("nan"), True, None, "1"],
)
def test_native_contexts_reject_invalid_idle_timeout_before_starting_a_reaper(timeout):
    with pytest.raises(ValueError, match="positive.*finite"):
        NativeContexts(lambda session: None, idle_timeout=timeout)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_all_acquisitions_including_queued_work_hold_the_lease_until_last_completion():
    clock = Clock()
    scheduled = []
    contexts = NativeContexts(scheduled.append, idle_timeout=10, clock=clock)
    first = contexts.acquire("a" * 32)
    queued = contexts.acquire("a" * 32)
    assert queued == first
    clock.now = 100
    contexts.complete("a" * 32, first)
    contexts.expire_idle()
    assert scheduled == []
    contexts.complete("a" * 32, queued)
    clock.now = 109.999
    contexts.expire_idle()
    assert scheduled == []
    clock.now = 110
    contexts.expire_idle()
    assert scheduled == [first]


def test_old_generation_completion_cannot_refresh_the_replacement_idle_deadline():
    clock = Clock()
    scheduled = []
    contexts = NativeContexts(scheduled.append, idle_timeout=10, clock=clock)
    old = contexts.acquire("a" * 32)
    assert contexts.release("a" * 32)
    assert scheduled == []
    clock.now = 1
    new = contexts.acquire("a" * 32)
    assert new != old
    clock.now = 2
    contexts.complete("a" * 32, new)
    clock.now = 11
    contexts.complete("a" * 32, old)
    assert scheduled == [old]
    contexts.complete("a" * 32, old)
    clock.now = 12
    contexts.expire_idle()
    assert scheduled == [old, new]


def test_release_all_detaches_active_generations_without_cleaning_before_completion():
    scheduled = []
    contexts = NativeContexts(scheduled.append)
    active = contexts.acquire("a" * 32)
    queued = contexts.acquire("a" * 32)
    idle = contexts.acquire("b" * 32)
    contexts.complete("b" * 32, idle)
    contexts.release_all()
    assert scheduled == [idle]
    assert not contexts.release("a" * 32)
    contexts.complete("a" * 32, active)
    assert scheduled == [idle]
    contexts.complete("a" * 32, queued)
    assert scheduled == [idle, active]
