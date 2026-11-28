"""Start, inspect, crash and stop the stores and the repair agent.

Stores and the agent are detached named actors, so they keep running between
commands. Every command attaches to an already running Ray cluster on this
machine, started beforehand with ``ray start --head``.

    python -m dtn.admin start           # start the S stores and the agent (no-op if running)
    python -m dtn.admin status          # ping every store; last repair round
    python -m dtn.admin repair          # run a repair round now
    python -m dtn.admin crash store-2   # kill -9 the process of one store (or "repair")
    python -m dtn.admin stop            # remove the stores and the agent for good
"""

import argparse
import json
import os
import signal
import sys
from dataclasses import replace
from datetime import datetime, timezone

import ray

from dtn.config import (
    DEFAULT_NAMESPACE,
    ConfigError,
    StorageConfig,
    check_cluster_params,
    load_namespace,
    load_storage_config,
)
from dtn.repair import ACTOR_NAME as REPAIR_NAME
from dtn.repair import RepairActor, repair_options
from dtn.store_actor import DEFAULT_CODEC, Codec, StoreActor, store_options

NAMESPACE = DEFAULT_NAMESPACE
# Records S and N in DTN_DATA_DIR the first time the stores start.
CLUSTER_FILE = "cluster.json"


def start_stores(
    config: StorageConfig,
    *,
    namespace: str = NAMESPACE,
    codec: Codec = DEFAULT_CODEC,
    timeout: float = 60.0,
) -> dict:
    """Start every store, or attach to the ones already running. Idempotent.

    Returns ``{logical name: actor handle}`` once every store answers a ping.
    Raises ``ConfigError`` if the data directory or the running stores use
    another S or N than ``config``.
    """
    # Workers may run in another directory: pass them an absolute path.
    config = replace(config, data_dir=config.data_dir.resolve())
    claim_data_dir(config)
    handles = {
        name: StoreActor.options(
            **store_options(name, config, namespace), get_if_exists=True
        ).remote(name, config, codec)
        for name in config.store_names
    }
    replies = ray.get([h.ping.remote() for h in handles.values()], timeout=timeout)
    # Stores already running keep the configuration they were started with.
    check_cluster_params(config, dict(zip(handles, replies)))
    return handles


def claim_data_dir(config: StorageConfig) -> None:
    """Record S and N in the data directory the first time; refuse others later.

    The data outlives the processes. Changing S would reorder the placement
    of every stored object, and changing N how many copies each must have.
    """
    path = config.data_dir / CLUSTER_FILE
    expected = config.cluster_params()
    if path.exists():
        recorded = json.loads(path.read_text())
        if recorded != expected:
            raise ConfigError(
                f"{path} records {recorded}, but this process uses {expected}. "
                "Start with the same DTN_NUM_STORES and DTN_REPLICAS, or use an empty DTN_DATA_DIR."
            )
        return
    config.data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(expected, sort_keys=True))
    os.replace(tmp, path)


def start_repair(config: StorageConfig, *, namespace: str = NAMESPACE, timeout: float = 60.0):
    """Start the repair agent, or attach to the running one. Start the stores first."""
    config = replace(config, data_dir=config.data_dir.resolve())
    handle = RepairActor.options(**repair_options(namespace), get_if_exists=True).remote(
        config, namespace
    )
    ray.get(handle.process_id.remote(), timeout=timeout)
    return handle


def get_repair(*, namespace: str = NAMESPACE):
    return ray.get_actor(REPAIR_NAME, namespace=namespace)


def get_stores(config: StorageConfig, *, namespace: str = NAMESPACE) -> dict:
    """Handles of the running stores, by logical name. Raises ``ValueError``
    if one was never started."""
    return {name: ray.get_actor(name, namespace=namespace) for name in config.store_names}


def crash_store(name: str, *, namespace: str = NAMESPACE) -> int:
    """Kill the process of a store (or of the agent, ``"repair"``) with SIGKILL,
    like a real crash, and return its PID.

    Unlike ``ray.kill``, the actor gets no chance to clean up, and Ray treats it
    as an unexpected death: it restarts the actor and fails calls in flight.
    """
    handle = ray.get_actor(name, namespace=namespace)
    pid = ray.get(handle.process_id.remote(), timeout=10)
    os.kill(pid, signal.SIGKILL)
    return pid


def stop_stores(config: StorageConfig, *, namespace: str = NAMESPACE) -> None:
    """Remove every store and the agent for good (no restart). Data stays on disk."""
    for name in (*config.store_names, REPAIR_NAME):
        try:
            ray.kill(ray.get_actor(name, namespace=namespace), no_restart=True)
        except ValueError:
            pass  # not running


def status(config: StorageConfig, *, namespace: str = NAMESPACE, timeout: float = 5.0) -> dict:
    """``{name: ping reply}`` for each store, or the error it gave."""
    result = {}
    for name in config.store_names:
        try:
            handle = ray.get_actor(name, namespace=namespace)
            result[name] = ray.get(handle.ping.remote(op_id="admin-status"), timeout=timeout)
        except Exception as e:  # report every failure, whatever its kind
            result[name] = f"{type(e).__name__}: {e}"
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dtn.admin", description=__doc__.splitlines()[0])
    parser.add_argument("--namespace", default=None, help="Ray namespace (default: DTN_RAY_NAMESPACE or dtn)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start", help="start the stores")
    commands.add_parser("status", help="ping every store; last repair round")
    commands.add_parser("repair", help="run a repair round now")
    crash = commands.add_parser("crash", help='kill -9 one store, or "repair"')
    crash.add_argument("store")
    commands.add_parser("stop", help="remove the stores and the agent")
    args = parser.parse_args(argv)

    try:
        config = load_storage_config()
        if args.namespace is None:
            args.namespace = load_namespace()
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 1
    try:
        ray.init(address="auto", namespace=args.namespace, log_to_driver=False)
    except ConnectionError:
        print("No Ray cluster is running here. Start one with: ray start --head", file=sys.stderr)
        return 1

    if args.command == "start":
        try:
            start_stores(config, namespace=args.namespace)
        except ConfigError as e:
            print(f"configuration error: {e}", file=sys.stderr)
            return 1
        start_repair(config, namespace=args.namespace)
        print(f"started {len(config.store_names)} stores and the repair agent in {config.data_dir.resolve()}")
        print(f"config {config.as_dict()}")
    elif args.command == "status":
        for name, reply in status(config, namespace=args.namespace).items():
            print(f"{name}  {reply}")
        try:
            report = ray.get(get_repair(namespace=args.namespace).last_report.remote(), timeout=5)
            print(f"repair  {report.as_dict() if report else 'no round yet'}")
        except Exception as e:
            print(f"repair  {type(e).__name__}: {e}")
    elif args.command == "repair":
        report = ray.get(get_repair(namespace=args.namespace).run_now.remote())
        print(f"{_now()} repair {report.as_dict()}")
    elif args.command == "crash":
        pid = crash_store(args.store, namespace=args.namespace)
        print(f"{_now()} crashed {args.store} (pid {pid}) with SIGKILL")
    elif args.command == "stop":
        stop_stores(config, namespace=args.namespace)
        print("stopped the stores and the repair agent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
