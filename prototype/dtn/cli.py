"""The ``dtn`` command (contract § 6, decision «Cómo se usa el sistema desde fuera»).

    python -m dtn upload FILE --title T --artist A --license L
    python -m dtn search TEXT
    python -m dtn list
    python -m dtn withdraw TRACK_ID --reason REASON
    python -m dtn play TRACK_ID [--headless]

Exit codes tell a script how the operation ended without reading text:
0 success, 2 ``Invalid``, 3 ``NotFound``, 4 ``Unavailable`` (uncertain outcome
on upload and withdraw), 5 ``Conflict``. Anything else (an operation that does
not exist yet, a bug) exits with 1. Every command accepts ``--json``.
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence

from dtn.client import Client, SearchResult
from dtn.errors import Conflict, DtnError, Invalid, NotFound, NotImplemented, Unavailable

EXIT_OK = 0
EXIT_OTHER = 1
EXIT_CODES = {Invalid: 2, NotFound: 3, Unavailable: 4, Conflict: 5}

ClientFactory = Callable[[], Client]


def default_client() -> Client:
    """Client wired to the real stores. Filled in when the Ray cluster exists (issue #5)."""
    raise NotImplemented("replicated storage is not available yet")


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
    if result.complete:
        print(f"{len(result.tracks)} pistas · resultado completo", file=out)
    else:
        print(f"{len(result.tracks)} pistas · resultado INCOMPLETO: no respondieron "
              f"{', '.join(result.unreachable)}", file=out)


def _error_exit(exc: Exception, as_json: bool, err) -> int:
    code = next((c for cls, c in EXIT_CODES.items() if isinstance(exc, cls)), EXIT_OTHER)
    if isinstance(exc, DtnError):
        name, message, op_id = type(exc).__name__, exc.message, exc.op_id
    else:
        name, message, op_id = "InternalError", repr(exc), None
    if as_json:
        print(json.dumps({"error": name, "message": message, "op_id": op_id}, ensure_ascii=False), file=err)
    else:
        suffix = f" [op {op_id}]" if op_id else ""
        hint = " (resultado incierto: repetir es seguro)" if isinstance(exc, Unavailable) else ""
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
