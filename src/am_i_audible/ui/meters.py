"""Live terminal VU meters + elapsed timer, rendered with rich.

Reads the per-track RMS levels exposed by the recorder and draws a bar per
track so the user can confirm *before and during* a meeting that both mic and
system audio are actually being captured -- the "am I audible?" check.
"""

from __future__ import annotations

import math

from rich.console import Group
from rich.live import Live
from rich.text import Text

from am_i_audible import config

_BAR_WIDTH = 30


def _dbfs(rms: float) -> float:
    return -math.inf if rms <= 1e-6 else 20.0 * math.log10(rms)


def _bar(rms: float) -> Text:
    # Map -60..0 dBFS onto the bar; colour by loudness.
    db = _dbfs(rms)
    frac = 0.0 if db == -math.inf else max(0.0, min(1.0, (db + 60.0) / 60.0))
    filled = int(frac * _BAR_WIDTH)
    colour = "green" if db < -12 else ("yellow" if db < -3 else "red")
    bar = Text()
    bar.append("█" * filled, style=colour)
    bar.append("░" * (_BAR_WIDTH - filled), style="grey37")
    label = "  -inf" if db == -math.inf else f"{db:5.1f}"
    bar.append(f" {label} dB", style="dim")
    return bar


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def render(recorder, mic_source: str, status: str = "") -> Group:
    """Build one frame of the meter display."""
    header = Text()
    header.append("● REC ", style="bold red")
    header.append(_clock(recorder.seconds), style="bold white")
    header.append("   mic: ", style="dim")
    header.append(mic_source, style="cyan")
    rows = [header, Text("")]
    for t in recorder.tracks:
        line = Text(f"{t.name:>7}  ", style="bold")
        line.append_text(_bar(t.level))
        rows.append(line)
    rows.append(Text(""))
    rows.append(Text(status or "[s] swap mic   [q]/Ctrl-C stop", style="dim"))
    return Group(*rows)


class MeterDisplay:
    """Context manager wrapping a rich.Live refresh loop."""

    def __init__(self, recorder, mic_source: str):
        self.recorder = recorder
        self.mic_source = mic_source
        self.status = ""
        self._live = Live(
            render(recorder, mic_source),
            refresh_per_second=config.METER_REFRESH_HZ,
            screen=False,
        )

    def update(self, mic_source: str | None = None) -> None:
        if mic_source is not None:
            self.mic_source = mic_source
        self._live.update(render(self.recorder, self.mic_source, self.status))

    def __enter__(self) -> "MeterDisplay":
        self._live.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self._live.__exit__(*exc)
