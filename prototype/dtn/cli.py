"""The ``dtn`` command (contract § 6, decision «Cómo se usa el sistema desde fuera»).

    python -m dtn upload FILE --title T --artist A --license L
    python -m dtn search TEXT
    python -m dtn list
    python -m dtn withdraw TRACK_ID --reason REASON
    python -m dtn play TRACK_ID [--headless]

Exit codes tell a script how the operation ended without reading text:
0 success, 2 ``Invalid``, 3 ``NotFound``, 4 ``Unavailable`` (uncertain outcome
on upload and withdraw; also when the stores are not running), 5 ``Conflict``.
Anything else (a configuration error, an operation that does not exist yet, a
bug) exits with 1. Every command accepts ``--json``.

The commands talk to the stores started with ``python -m dtn.admin start``,
through the Ray cluster running on this machine.
"""

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence

from dtn.client import Client, SearchResult
from dtn.config import ConfigError, load_client_config, load_namespace, load_storage_config
from dtn.errors import Conflict, DtnError, Invalid, NotFound, NotImplemented, Unavailable

EXIT_OK = 0
EXIT_OTHER = 1
EXIT_CODES = {Invalid: 2, NotFound: 3, Unavailable: 4, Conflict: 5}

ClientFactory = Callable[[], Client]


def default_client() -> Client:
    """Client wired to the stores started with ``python -m dtn.admin start``.

    Raises ``Unavailable`` if no Ray cluster or no stores are running here, and
    ``ConfigError`` if the configuration is invalid or its S and N differ from
    those of the running stores.
    """
    # Imported here so that the rest of the command, and its tests, run without Ray.
    import ray

    from dtn.ray_cluster import RayCluster

    storage = load_storage_config()
    config = load_client_config()
    namespace = load_namespace()
    if not ray.is_initialized():
        try:
            ray.init(address="auto", namespace=namespace, log_to_driver=False, logging_level=logging.ERROR)
        except ConnectionError:
            raise Unavailable(
                "no Ray cluster is running on this machine: start one with `ray start --head`, "
                "then the stores with `python -m dtn.admin start`"
            ) from None
    try:
        cluster = RayCluster.connect(storage, namespace=namespace)
    except ConfigError:
        raise
    except ValueError:  # a store actor does not exist
        raise Unavailable(
            f"the stores are not running in namespace {namespace!r}: "
            "start them with `python -m dtn.admin start`"
        ) from None
    return Client(cluster, config)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dtn", description="drop-the-node: fault-tolerant audio streaming")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload", help="upload a track and print its id")
    up.add_argument("file")
    up.add_argument("--title", required=True)
    up.add_argument("--artist", required=True)
    up.add_argument("--license", required=True)

    se = sub.add_parser("search", help="tracks whose title or artist contain every word")
    se.add_argument("text", nargs="*")

    sub.add_parser("list", help="every visible track")

    wd = sub.add_parser("withdraw", help="withdraw a track for good")
    wd.add_argument("track_id")
    wd.add_argument("--reason", required=True)

    pl = sub.add_parser("play", help="play a track")
    pl.add_argument("track_id")
    pl.add_argument("--headless", action="store_true")

    for p in (up, se, wd, pl, sub.choices["list"]):
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    return parser


def _format_duration(seconds: float) -> str:
    total = round(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _print_search(result: SearchResult, as_json: bool, out) -> None:
    if as_json:
        doc = {
            "tracks": [t.__dict__ for t in result.tracks],
            "complete": result.complete,
            "unreachable": list(result.unreachable),
        }
        print(json.dumps(doc, ensure_ascii=False), file=out)
        return
    for t in result.tracks:
        print(f"{t.track_id}  {t.artist} — {t.title}  ({_format_duration(t.duration)}, {t.license})", file=out)
    count = f"{len(result.tracks)} track{'' if len(result.tracks) == 1 else 's'}"
    if result.complete:
        print(f"{count} · complete result", file=out)
    else:
        print(f"{count} · INCOMPLETE result: no answer from {', '.join(result.unreachable)}", file=out)


def _error_exit(exc: Exception, as_json: bool, err) -> int:
    code = next((c for cls, c in EXIT_CODES.items() if isinstance(exc, cls)), EXIT_OTHER)
    if isinstance(exc, DtnError):
        name, message, op_id = type(exc).__name__, exc.message, exc.op_id
    elif isinstance(exc, ConfigError):
        name, message, op_id = "ConfigError", str(exc), None
    else:
        name, message, op_id = "InternalError", repr(exc), None
    if as_json:
        print(json.dumps({"error": name, "message": message, "op_id": op_id}, ensure_ascii=False), file=err)
    else:
        suffix = f" [op {op_id}]" if op_id else ""
        # Only an operation that started (it has an op_id) can leave an uncertain outcome.
        uncertain = isinstance(exc, Unavailable) and op_id is not None
        hint = " (uncertain outcome: repeating is safe)" if uncertain else ""
        print(f"error: {name}: {message}{suffix}{hint}", file=err)
    return code


def main(argv: Sequence[str] | None = None, *, client_factory: ClientFactory = default_client,
         out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = _parser().parse_args(argv)
    as_json = args.json
    try:
        if args.command == "play":
            raise NotImplemented("play is not implemented yet")
        client = client_factory()
        if args.command == "upload":
            track_id = client.upload(args.file, {"title": args.title, "artist": args.artist, "license": args.license})
            print(json.dumps({"track_id": track_id}) if as_json else track_id, file=out)
        elif args.command == "search":
            _print_search(client.search(" ".join(args.text)), as_json, out)
        elif args.command == "list":
            _print_search(client.list(), as_json, out)
        elif args.command == "withdraw":
            client.withdraw(args.track_id, args.reason)
            if as_json:
                print(json.dumps({"withdrawn": args.track_id}), file=out)
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - every failure becomes an exit code
        return _error_exit(exc, as_json, err)


if __name__ == "__main__":
    sys.exit(main())
