"""Ray behaviours the prototype depends on.

These tests do not exercise our own code. Each one pins a behaviour of the
Ray runtime that the design relies on, so that the middleware dependency map
is backed by executable evidence. If Ray changes any of these behaviours,
a test here fails before our code does.
"""

import os
import time

import pytest
import ray
from ray.exceptions import ActorDiedError, GetTimeoutError, RayActorError


@ray.remote
class Box:
    """Minimal stateful actor used to observe Ray's actor semantics."""

    def __init__(self):
        self.items = {}

    def put(self, key, value):
        self.items[key] = value

    def get(self, key):
        return self.items[key]

    def pid(self):
        return os.getpid()

    def slow(self, seconds):
        time.sleep(seconds)
        return "done"


def test_actor_keeps_state_between_calls(ray_runtime):
    """An actor remembers what earlier calls stored in its memory."""
    box = Box.remote()
    ray.get(box.put.remote("k", b"bytes"))
    assert ray.get(box.get.remote("k")) == b"bytes"


def test_actor_runs_in_its_own_process(ray_runtime):
    """An actor lives in a process different from the driver."""
    box = Box.remote()
    assert ray.get(box.pid.remote()) != os.getpid()


def test_remote_call_returns_before_the_work_finishes(ray_runtime):
    """.remote() returns an ObjectRef immediately; only ray.get waits."""
    box = Box.remote()
    ray.get(box.pid.remote())  # make sure the actor is already running

    start = time.monotonic()
    ref = box.slow.remote(1.0)
    submit_seconds = time.monotonic() - start

    assert submit_seconds < 0.5
    assert ray.get(ref) == "done"


def test_get_with_timeout_raises_when_the_actor_is_slow(ray_runtime):
    """Without a timeout, ray.get would wait forever for a hung actor."""
    box = Box.remote()
    with pytest.raises(GetTimeoutError):
        ray.get(box.slow.remote(2.0), timeout=0.2)


def test_killed_actor_raises_actor_died_error(ray_runtime):
    """Calling a dead actor raises ActorDiedError, a kind of RayActorError."""
    box = Box.remote()
    ray.get(box.put.remote("k", b"bytes"))
    ray.kill(box)

    with pytest.raises(ActorDiedError) as excinfo:
        ray.get(box.get.remote("k"), timeout=10)
    assert isinstance(excinfo.value, RayActorError)


def test_killed_actor_is_not_restarted_by_default(ray_runtime):
    """With the default max_restarts=0, a dead actor stays dead: every call fails."""
    box = Box.remote()
    ray.kill(box)

    for _ in range(2):
        with pytest.raises(ActorDiedError):
            ray.get(box.pid.remote(), timeout=10)
