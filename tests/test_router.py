"""Unit tests for AudioRouter wiring, hot-swap overlap, and teardown.

Uses an injected fake backend (no real audio system) and stdlib unittest, so it
runs anywhere with `python -m unittest`.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from am_i_audible import config  # noqa: E402
from am_i_audible.audio.backends import (  # noqa: E402
    Handle,
    PactlBackend,
    PipeWireBackend,
)
from am_i_audible.audio.router import AudioRouter  # noqa: E402


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.events = []          # ordered log of operations
        self.live = []            # currently-existing handles
        self._n = 0

    def default_source(self):
        return "mic0"

    def default_sink(self):
        return "sink0"

    def monitor_of(self, sink_name):
        return f"{sink_name}.monitor"

    def list_sources(self):
        return ["mic0", "mic1", config.SINK_MIC]

    def _make(self, kind, label):
        self._n += 1
        h = Handle(kind=kind, payload=self._n, label=label)
        self.live.append(h)
        return h

    def create_null_sink(self, name, description):
        self.events.append(("create_sink", name))
        return self._make("module", f"sink:{name}")

    def route(self, source, sink):
        self.events.append(("route", source, sink))
        return self._make("module", f"loop:{source}->{sink}")

    def destroy(self, handle):
        self.events.append(("destroy", handle.label))
        if handle in self.live:
            self.live.remove(handle)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.be = FakeBackend()
        self.router = AudioRouter(backend=self.be)

    def test_setup_creates_two_sinks_and_two_routes(self):
        routes = self.router.setup()
        self.assertEqual(routes.mic_monitor, f"{config.SINK_MIC}.monitor")
        self.assertEqual(routes.system_monitor, f"{config.SINK_SYSTEM}.monitor")
        kinds = [e[0] for e in self.be.events]
        self.assertEqual(kinds.count("create_sink"), 2)
        self.assertEqual(kinds.count("route"), 2)
        self.assertEqual(len(self.be.live), 4)

    def test_swap_mic_creates_new_before_destroying_old(self):
        self.router.setup()
        self.be.events.clear()
        self.router.swap_mic("mic1")
        # the new route must be created before the old loopback is destroyed
        kinds = [e[0] for e in self.be.events]
        self.assertLess(kinds.index("route"), kinds.index("destroy"))
        self.assertEqual(self.router.current_mic, "mic1")
        self.assertEqual(len(self.be.live), 4)  # still 2 sinks + 2 loopbacks

    def test_swap_mic_noop_when_same_source(self):
        self.router.setup()
        self.be.events.clear()
        self.router.swap_mic("mic0")  # already the current mic
        self.assertEqual(self.be.events, [])

    def test_teardown_destroys_all_in_reverse_and_is_idempotent(self):
        self.router.setup()
        created = [h.label for h in self.be.live]
        self.be.events.clear()
        self.router.teardown()
        destroyed = [e[1] for e in self.be.events if e[0] == "destroy"]
        self.assertEqual(destroyed, list(reversed(created)))
        self.assertEqual(self.be.live, [])
        self.be.events.clear()
        self.router.teardown()  # second call must be a no-op
        self.assertEqual(self.be.events, [])

    def test_list_microphones_excludes_our_objects(self):
        self.router.setup()
        mics = self.router.list_microphones()
        self.assertIn("mic0", mics)
        self.assertNotIn(config.SINK_MIC, mics)


# --------------------------------------------------------------------------- #
# Backend unit tests (no audio hardware required)
# --------------------------------------------------------------------------- #

class PactlBackendTests(unittest.TestCase):
    """PactlBackend.monitor_of must always append .monitor."""

    def setUp(self):
        self.be = PactlBackend()

    def test_monitor_of_our_sink(self):
        self.assertEqual(self.be.monitor_of(config.SINK_MIC),
                         f"{config.SINK_MIC}.monitor")

    def test_monitor_of_our_system_sink(self):
        self.assertEqual(self.be.monitor_of(config.SINK_SYSTEM),
                         f"{config.SINK_SYSTEM}.monitor")

    def test_monitor_of_external_sink(self):
        self.assertEqual(self.be.monitor_of("alsa_output.pci-0000_00_1f.3.analog-stereo"),
                         "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor")

    def test_capture_uses_parec_with_device(self):
        """pactl null-sink monitors are PulseAudio source names that
        pw-record cannot resolve (it falls back to the default mic, making
        every track identical). The pactl backend must capture with parec
        --device, which resolves the monitor source correctly."""
        argv = self.be.capture_argv(f"{config.SINK_SYSTEM}.monitor")
        self.assertEqual(argv[0], "parec")
        self.assertIn(f"--device={config.SINK_SYSTEM}.monitor", argv)


class PipeWireBackendTests(unittest.TestCase):
    """PipeWireBackend.monitor_of must always append .monitor (same as pactl)."""

    def setUp(self):
        self.be = PipeWireBackend()

    def test_monitor_of_our_sink(self):
        self.assertEqual(self.be.monitor_of(config.SINK_MIC),
                         f"{config.SINK_MIC}.monitor")

    def test_monitor_of_our_system_sink(self):
        self.assertEqual(self.be.monitor_of(config.SINK_SYSTEM),
                         f"{config.SINK_SYSTEM}.monitor")

    def test_monitor_of_external_sink(self):
        """System audio capture: monitor_of(default_sink) must return the
        monitor source name, NOT the sink name itself — otherwise the
        route() call would link the sink's playback ports instead of the
        monitor's capture ports, producing a silent system track."""
        self.assertEqual(self.be.monitor_of("alsa_output.pci-0000_00_1f.3.analog-stereo"),
                         "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor")

    def test_capture_uses_pw_record(self):
        """The pipewire-native backend's monitor IS a real PipeWire node
        (the pw-loopback playback end), so pw-record --target resolves it."""
        argv = self.be.capture_argv(f"{config.SINK_SYSTEM}.monitor")
        self.assertEqual(argv[0], "pw-record")
        self.assertIn("--target", argv)
        self.assertIn(f"{config.SINK_SYSTEM}.monitor", argv)


class SystemRouteTests(unittest.TestCase):
    """Verify that the system audio route uses the monitor of the default
    sink, not the sink itself."""

    def setUp(self):
        self.be = FakeBackend()
        self.router = AudioRouter(backend=self.be)

    def test_system_route_source_is_monitor_of_default_sink(self):
        self.router.setup()
        # System route is created BEFORE mic route in router.setup():
        # 1. route(monitor_of(default_sink), SINK_SYSTEM)  <- system
        # 2. route(default_source, SINK_MIC)                <- mic
        route_events = [e for e in self.be.events if e[0] == "route"]
        self.assertEqual(len(route_events), 2)
        sys_source, sys_sink = route_events[0][1], route_events[0][2]
        mic_source, mic_sink = route_events[1][1], route_events[1][2]
        self.assertEqual(sys_source, f"{self.be.default_sink()}.monitor",
                         "system route source must be the monitor of the default sink")
        self.assertEqual(sys_sink, config.SINK_SYSTEM)
        self.assertEqual(mic_source, self.be.default_source(),
                         "mic route source must be the default source")
        self.assertEqual(mic_sink, config.SINK_MIC)

    def test_system_route_not_same_as_mic_route(self):
        """Mic routes default_source -> SINK_MIC, system routes
        monitor_of(default_sink) -> SINK_SYSTEM. They must use different
        sources and different sinks."""
        self.router.setup()
        route_events = [e for e in self.be.events if e[0] == "route"]
        mic_source, mic_sink = route_events[0][1], route_events[0][2]
        sys_source, sys_sink = route_events[1][1], route_events[1][2]
        self.assertNotEqual(mic_source, sys_source,
                            "mic and system must use different sources")
        self.assertNotEqual(mic_sink, sys_sink,
                            "mic and system must use different sinks")


if __name__ == "__main__":
    unittest.main()
