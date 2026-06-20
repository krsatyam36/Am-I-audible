"""Entry point for ``python -m am_i_audible``.

v0.1.0 is being built incrementally. The full CLI (cli.py: record session,
VU meters, hot-swap) lands in the next step. For now this points at the audio
routing test harness so the routing layer can be verified in isolation.
"""

import sys


def main() -> int:
    print(
        "am-I-audible v0.1.0 (capture foundation -- in progress)\n\n"
        "The recording CLI is not wired up yet. To test the audio routing "
        "layer that has been built so far, run:\n\n"
        "    python -m am_i_audible.audio.router\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
