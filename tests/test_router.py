"""Unit tests for AudioRouter wiring, hot-swap overlap, and teardown.

Uses an injected fake backend (no real audio system) and stdlib unittest, so it
runs anywhere with `python -m unittest`.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from am_i_audible import config  # noqa: E402
from am_i_audible.audio.backends import Handle  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
