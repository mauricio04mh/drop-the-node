"""Ray behaviours the prototype depends on.

These tests do not exercise our own code. Each one pins a behaviour of the
Ray runtime that the design relies on, so that the middleware dependency map
is backed by executable evidence. If Ray changes any of these behaviours,
a test here fails before our code does.
"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest
import ray
from ray.exceptions import (
    ActorDiedError,
    GetTimeoutError,
    RayActorError,
    TaskCancelledError,
)

from dtn.errors import Unavailable

from conftest import RAY_NAMESPACE


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

    def slow_put(self, seconds, key):
        """Sleep, then leave a visible side effect."""
        time.sleep(seconds)
        self.items[key] = True
        return "done"

    def keys(self):
        return sorted(self.items)

    def fail_unavailable(self):
        raise Unavailable("store is busy", saturated=True, op_id="op-7")


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


def test_application_error_reaches_the_driver_with_its_class_and_fields(ray_runtime):
    """An error raised inside an actor can be caught in the driver by its own class.

    The client tells NotFound, Unavailable and Corrupt apart by type (P5), and
    reads fields such as ``saturated``, so both must survive the trip.
    """
    box = Box.remote()
    with pytest.raises(Unavailable) as excinfo:
        ray.get(box.fail_unavailable.remote(), timeout=10)
    assert excinfo.value.saturated is True
    assert excinfo.value.op_id == "op-7"


def _crash(pid):
    """Kill a process the way a real crash would: no cleanup, no goodbye."""
    os.kill(pid, signal.SIGKILL)


def test_call_in_flight_fails_when_the_actor_process_crashes(ray_runtime):
    """A pending call to a crashed actor fails instead of waiting forever.

    Switching to another replica relies on noticing the failure.
    """
    box = Box.remote()
    pid = ray.get(box.pid.remote())
    pending = box.slow.remote(30.0)
    time.sleep(0.3)
    _crash(pid)

    with pytest.raises(RayActorError):
        ray.get(pending, timeout=20)


def test_wait_with_timeout_leaves_unfinished_calls_running(ray_runtime):
    """ray.wait returns what is not ready yet without cancelling it.

    A hedged read sends a second request while the first one keeps running.
    """
    box = Box.remote()
    first = box.slow.remote(1.0)

    ready, not_ready = ray.wait([first], timeout=0.1)
    assert ready == []
    assert not_ready == [first]
    assert ray.get(first, timeout=10) == "done"


@pytest.mark.parametrize("attempt", range(20))
def test_cancel_prevents_a_queued_call_from_running_on_a_threaded_actor(ray_runtime, attempt):
    """On an actor with max_concurrency > 1, a cancelled call that was still
    waiting for a thread never runs, and the actor keeps answering at once.
    The error only arrives when the actor reaches the call in its queue.

    Hedged reads rely on this: stores are threaded actors, and the losing
    request is cancelled. Repeated because the outcome of a cancel depends
    on whether the call already reached the actor.
    """
    box = Box.options(max_concurrency=2).remote()
    ray.get(box.pid.remote())
    busy = [box.slow.remote(0.5) for _ in range(2)]
    if attempt % 2:
        time.sleep(0.1)  # half the attempts cancel after the call was sent
    queued = box.slow_put.remote(0.0, "queued")
    ray.cancel(queued)

    with pytest.raises(TaskCancelledError):
        ray.get(queued, timeout=10)
    ray.get(busy, timeout=10)
    start = time.monotonic()
    assert ray.get(box.keys.remote(), timeout=10) == []
    assert time.monotonic() - start < 1.0


# Runs in its own Ray instance: the reorder wait is a system setting, fixed
# when Ray starts, and lowering it keeps the stalls short.
_ORDERED_CANCEL_SCRIPT = """
import json, time
import ray
from ray.exceptions import TaskCancelledError

ray.init(address="local", num_cpus=2, include_dashboard=False, log_to_driver=False,
         _system_config={"actor_scheduling_queue_max_reorder_wait_seconds": 3})

@ray.remote
class Box:
    def __init__(self):
        self.items = {}
    def pid(self):
        return 1
    def slow(self, seconds):
        time.sleep(seconds)
    def slow_put(self, seconds, key):
        time.sleep(seconds)
        self.items[key] = True
    def keys(self):
        return sorted(self.items)

attempts = []
for _ in range(10):
    box = Box.remote()
    ray.get(box.pid.remote())
    busy = box.slow.remote(0.5)
    time.sleep(0.1)
    queued = box.slow_put.remote(0.0, "queued")
    ray.cancel(queued)
    try:
        ray.get(queued, timeout=30)
        result = "returned"
    except TaskCancelledError:
        result = "cancelled"
    ray.get(busy, timeout=30)
    try:
        ran = "queued" in ray.get(box.keys.remote(), timeout=30)
        after = "answered"
    except Exception as e:
        ran, after = None, type(e).__name__
    attempts.append({"result": result, "ran": ran, "next_call": after})
    ray.kill(box)
print("ATTEMPTS", json.dumps(attempts))
"""


def test_cancelling_a_queued_call_on_a_single_threaded_actor_is_unreliable(ray_runtime):
    """On a default actor, calls run in the order they were sent, numbered by
    Ray. Cancelling a call that waits in the queue does not do what the Ray
    documentation says ("cancelled and not executed"). Either:

    - the call had already reached the actor, and it runs anyway; only its
      result becomes TaskCancelledError; or
    - it had not left the caller yet, so it never arrives. The actor waits
      for the missing number (30 s by default), then drops every queued call:
      the next one fails with ActorUnavailableError.

    Which one happens is a race. Cancelling is therefore never part of
    correctness, and stores must stay threaded actors (see the test above).
    """
    result = subprocess.run(
        [sys.executable, "-c", _ORDERED_CANCEL_SCRIPT],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    line = next(l for l in result.stdout.splitlines() if l.startswith("ATTEMPTS "))
    attempts = json.loads(line.removeprefix("ATTEMPTS "))

    assert len(attempts) == 10
    for attempt in attempts:
        assert attempt["result"] == "cancelled"
        ran_anyway = attempt["ran"] is True
        dropped_queue = attempt["next_call"] == "ActorUnavailableError"
        assert ran_anyway or dropped_queue, attempt


def test_cancel_does_not_interrupt_a_running_call_on_a_threaded_actor(ray_runtime):
    """Cancelling a call that already started only discards its result.

    The call runs to the end and its side effects happen, so the losing
    request of a hedged read keeps its store busy until it finishes.
    """
    box = Box.options(max_concurrency=4).remote()
    ray.get(box.pid.remote())
    running = box.slow_put.remote(1.0, "running")
    time.sleep(0.2)
    ray.cancel(running)

    with pytest.raises(TaskCancelledError):
        ray.get(running, timeout=10)
    time.sleep(1.2)
    assert ray.get(box.keys.remote(), timeout=10) == ["running"]


def test_restarted_actor_runs_its_constructor_again_and_keeps_its_name(ray_runtime):
    """With max_restarts, a crashed actor comes back in a new process, with
    empty memory, under the same name. A store rebuilds its index from disk
    in the constructor for this reason."""
    box = Box.options(name="restartable", max_restarts=1).remote()
    first_pid = ray.get(box.pid.remote())
    ray.get(box.put.remote("k", "v"))
    _crash(first_pid)

    deadline = time.monotonic() + 30
    while True:
        try:
            new_pid = ray.get(box.pid.remote(), timeout=10)
            break
        except RayActorError:
            assert time.monotonic() < deadline, "actor did not restart"
            time.sleep(0.2)

    assert new_pid != first_pid
    assert ray.get(box.keys.remote()) == []
    assert ray.get(ray.get_actor("restartable").pid.remote()) == new_pid


def test_calls_in_flight_fail_even_if_the_actor_restarts(ray_runtime):
    """With the default max_task_retries=0, Ray does not replay a call on the
    restarted actor: both the call in flight and one sent during the restart
    fail. The client decides whether to retry, because only it knows whether
    the call is safe to repeat."""
    box = Box.options(max_restarts=1).remote()
    pid = ray.get(box.pid.remote())
    in_flight = box.slow.remote(30.0)
    time.sleep(0.3)
    _crash(pid)
    during_restart = box.pid.remote()

    with pytest.raises(RayActorError):
        ray.get(in_flight, timeout=20)
    with pytest.raises(RayActorError):
        ray.get(during_restart, timeout=20)


def test_threaded_actor_runs_calls_concurrently(ray_runtime):
    """With max_concurrency=K, up to K calls run at the same time.

    A store serves several requests at once, so its index needs a lock.
    """
    box = Box.options(max_concurrency=4).remote()
    ray.get(box.pid.remote())

    start = time.monotonic()
    ray.get([box.slow.remote(0.5) for _ in range(4)], timeout=10)
    assert time.monotonic() - start < 1.5


def test_threaded_actor_queues_calls_beyond_its_limit(ray_runtime):
    """Call K + 1 waits for a free thread instead of being rejected.

    Ray gives no "busy" answer, so a store that must reject when saturated
    keeps its own count of calls in progress and uses a max_concurrency
    larger than that limit, so the rejection itself gets a thread to run.
    """
    box = Box.options(max_concurrency=2).remote()
    ray.get(box.pid.remote())

    start = time.monotonic()
    results = ray.get([box.slow.remote(0.5) for _ in range(3)], timeout=10)
    assert results == ["done"] * 3
    assert time.monotonic() - start >= 0.9


def test_named_actor_is_reachable_from_another_process(ray_runtime):
    """Another driver attached to the same cluster finds an actor by name.

    The script that crashes stores during experiments works this way.
    """
    box = Box.options(name="findable").remote()
    pid = ray.get(box.pid.remote())
    code = (
        "import ray\n"
        f"ray.init(address='auto', namespace='{RAY_NAMESPACE}', log_to_driver=False)\n"
        "print('PID', ray.get(ray.get_actor('findable').pid.remote()))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert f"PID {pid}" in result.stdout
