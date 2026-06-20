"""Command-line interface for am-I-audible v0.1.0.

    am-i-audible record [--label NAME] [--mic-only | --system-only]
    am-i-audible devices
"""

from __future__ import annotations

import argparse
import logging
import sys

from am_i_audible import __version__, config
from am_i_audible.audio.router import AudioRouter, RoutingError


def _cmd_record(args: argparse.Namespace) -> int:
    from am_i_audible.core.session import RecordingSession

    session = RecordingSession(
        label=args.label,
        record_mic=not args.system_only,
        record_system=not args.mic_only,
        duration=args.duration,
    )
    print(f"am-I-audible {__version__}  |  recording to {config.RECORDINGS_ROOT}")
    print("Speak / play audio to see the meters move. [s] swap mic, [q] stop.\n")
    try:
        session.run()
    except RoutingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_devices(_args: argparse.Namespace) -> int:
    router = AudioRouter()
    print(f"backend: {router.backend_name}")
    try:
        print(f"default mic   : {router._backend.default_source()}")
        print(f"default sink  : {router._backend.default_sink()}")
    except RoutingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\navailable microphone sources:")
    for src in router._backend.list_sources():
        if not src.startswith(config.OBJECT_PREFIX):
            print(f"  {src}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="am-i-audible",
        description="Linux dual-track meeting recorder (mic + system audio).",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command")

    rec = sub.add_parser("record", help="record mic + system audio (default)")
    rec.add_argument("--label", help="label appended to the session folder name")
    rec.add_argument("--duration", type=float, metavar="SECONDS",
                     help="stop automatically after N seconds")
    grp = rec.add_mutually_exclusive_group()
    grp.add_argument("--mic-only", action="store_true", help="record only the mic")
    grp.add_argument("--system-only", action="store_true",
                     help="record only system audio")
    rec.set_defaults(func=_cmd_record)

    dev = sub.add_parser("devices", help="show backend + audio sources")
    dev.set_defaults(func=_cmd_devices)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not getattr(args, "command", None):
        # default to `record` for a bare invocation
        args = parser.parse_args(["record", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
