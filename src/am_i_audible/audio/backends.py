"""System-command backends that create/route/destroy virtual audio objects.

Two interchangeable backends implement the same small interface so ``router.py``
stays backend-agnostic:

* :class:`PactlBackend`     -- uses ``pactl`` (PulseAudio / pipewire-pulse).
                               Preferred: simplest, cleanest teardown via module ids.
* :class:`PipeWireBackend`  -- uses native ``pw-loopback`` + ``pw-link`` + ``wpctl``.
                               Fallback when ``pactl`` is absent (no ``pulseaudio-utils``).

:func:`detect_backend` returns the best available one. Every subprocess call goes
through :func:`_run`, which is the single seam unit tests mock.
"""

from __future__ import annotations

import logging
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from am_i_audible import config

log = logging.getLogger(__name__)


class RoutingError(RuntimeError):
    """Raised when a backend command fails or no backend is available."""


@dataclass
class Handle:
    """Opaque reference to one created object, tagged with how to destroy it.

    ``kind`` is backend-specific:
      * pactl     -> kind="module",  payload=<module id str>
      * pipewire  -> kind="process", payload=<subprocess.Popen> (a pw-loopback)
                  -> kind="link",    payload=(output_name, input_name)
    """

    kind: str
    payload: object
    label: str = ""


def _run(cmd: list[str], *, check: bool = True) -> str:
    """Run ``cmd`` and return stdout (stripped). Raise RoutingError on failure."""
    log.debug("exec: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RoutingError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


# --------------------------------------------------------------------------- #
# pactl backend                                                               #
# --------------------------------------------------------------------------- #
class PactlBackend:
    name = "pactl"

    def default_source(self) -> str:
        return _run(["pactl", "get-default-source"])

    def default_sink(self) -> str:
        return _run(["pactl", "get-default-sink"])

    def monitor_of(self, sink_name: str) -> str:
        return f"{sink_name}.monitor"

    def list_sources(self) -> list[str]:
        out = _run(["pactl", "list", "short", "sources"])
        return [line.split("\t")[1] for line in out.splitlines() if "\t" in line]

    def create_null_sink(self, name: str, description: str) -> Handle:
        module_id = _run([
            "pactl", "load-module", "module-null-sink",
            f"sink_name={name}",
            # single quotes are kept literal in this one argv token and let the
            # value contain spaces/parens; pactl strips them when parsing.
            f"sink_properties=device.description='{description}'",
        ])
        log.info("pactl: created null sink %s (module %s)", name, module_id)
        return Handle(kind="module", payload=module_id, label=f"sink:{name}")

    def route(self, source_name: str, sink_name: str) -> Handle:
        module_id = _run([
            "pactl", "load-module", "module-loopback",
            f"source={source_name}",
            f"sink={sink_name}",
            f"latency_msec={config.LOOPBACK_LATENCY_MS}",
            # keep the loopback pinned to our sink even if defaults change.
            "sink_dont_move=true",
        ])
        log.info("pactl: routed %s -> %s (module %s)", source_name, sink_name, module_id)
        return Handle(kind="module", payload=module_id,
                      label=f"loopback:{source_name}->{sink_name}")

    def destroy(self, handle: Handle) -> None:
        if handle.kind != "module":
            return
        _run(["pactl", "unload-module", str(handle.payload)], check=False)
        log.info("pactl: unloaded module %s (%s)", handle.payload, handle.label)


# --------------------------------------------------------------------------- #
# PipeWire-native backend                                                     #
# --------------------------------------------------------------------------- #
class PipeWireBackend:
    """Null sinks are long-lived ``pw-loopback`` processes; routing is ``pw-link``.

    A ``pw-loopback`` whose *capture* end is an ``Audio/Sink`` and whose *playback*
    end is an ``Audio/Source`` gives us exactly a null sink + a stable monitor
    source, with nothing leaking to the speakers. Killing the process removes the
    sink, its monitor, and every link into it -- so teardown is just terminate().
    """

    name = "pipewire"

    def _inspect_default(self, token: str) -> str:
        """Resolve @DEFAULT_AUDIO_SOURCE@ / @DEFAULT_AUDIO_SINK@ to a node.name."""
        out = _run(["wpctl", "inspect", token])
        for line in out.splitlines():
            line = line.strip().lstrip("*").strip()
            if line.startswith("node.name"):
                # form:  node.name = "alsa_input.pci-..."
                return line.split("=", 1)[1].strip().strip('"')
        raise RoutingError(f"could not resolve node.name for {token}")

    def default_source(self) -> str:
        return self._inspect_default("@DEFAULT_AUDIO_SOURCE@")

    def default_sink(self) -> str:
        return self._inspect_default("@DEFAULT_AUDIO_SINK@")

    def monitor_of(self, sink_name: str) -> str:
        if sink_name.startswith(config.OBJECT_PREFIX):
            return f"{sink_name}.monitor"
        return sink_name

    def list_sources(self) -> list[str]:
        # Best-effort via pw-dump JSON; empty list if unavailable.
        if not shutil.which("pw-dump"):
            return []
        import json
        try:
            data = json.loads(_run(["pw-dump"]))
        except (RoutingError, json.JSONDecodeError):
            return []
        names = []
        for obj in data:
            props = (obj.get("info") or {}).get("props") or {}
            if props.get("media.class") in ("Audio/Source", "Audio/Source/Virtual"):
                name = props.get("node.name")
                if name:
                    names.append(name)
        return names

    def create_null_sink(self, name: str, description: str) -> Handle:
        monitor = self.monitor_of(name)
        # Remove any orphaned loopback still holding this sink name (e.g. from a
        # previous run that was Ctrl-Z'd, crashed, or SIGKILLed). A leftover —
        # especially a *suspended* one — can't pass audio yet blocks the name, so
        # a fresh sink would route into a dead duplicate (silent capture).
        if shutil.which("pkill"):
            subprocess.run(["pkill", "-9", "-f", f"node.name={name} "],
                           capture_output=True)
        proc = subprocess.Popen(
            [
                "pw-loopback",
                "--capture-props",
                f'media.class=Audio/Sink node.name={name} '
                f'node.description="{description}"',
                "--playback-props",
                f'media.class=Audio/Source node.name={monitor} '
                f'node.description="{description} monitor"',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("pipewire: spawned pw-loopback for sink %s (pid %s)", name, proc.pid)
        return Handle(kind="process", payload=proc, label=f"sink:{name}")

    def route(self, source_name: str, sink_name: str) -> Handle:
        # Enumerate output ports of the source and input ports of the sink,
        # then link them 1:1 positionally.
        outputs = [p for p in _run(["pw-link", "-o"]).splitlines()
                   if p.startswith(f"{source_name}:")]
        inputs = [p for p in _run(["pw-link", "-i"]).splitlines()
                  if p.startswith(f"{sink_name}:")]
        n = min(len(outputs), len(inputs))
        for i in range(n):
            _run(["pw-link", outputs[i], inputs[i]], check=False)
        log.info("pipewire: linked %d port(s) %s -> %s", n, source_name, sink_name)
        return Handle(kind="link", payload=(source_name, sink_name),
                      label=f"link:{source_name}->{sink_name}")

    def destroy(self, handle: Handle) -> None:
        if handle.kind == "process":
            proc: subprocess.Popen = handle.payload  # type: ignore[assignment]
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            log.info("pipewire: terminated %s", handle.label)
        elif handle.kind == "link":
            src, dst = handle.payload  # type: ignore[misc]
            _run(["pw-link", "-d", src, dst], check=False)
            log.info("pipewire: unlinked %s", handle.label)


# --------------------------------------------------------------------------- #
# selection                                                                   #
# --------------------------------------------------------------------------- #
def detect_backend():
    """Return the preferred available backend instance.

    Prefers pactl (simplest, cleanest teardown); falls back to PipeWire-native.
    """
    if shutil.which("pactl"):
        log.info("using pactl backend")
        return PactlBackend()
    if shutil.which("pw-loopback") and shutil.which("pw-link"):
        log.info("pactl not found; using PipeWire-native backend "
                 "(install `pulseaudio-utils` for the simpler pactl path)")
        return PipeWireBackend()
    raise RoutingError(
        "no usable audio backend: install `pulseaudio-utils` (pactl) "
        "or ensure pw-loopback/pw-link are present"
    )
