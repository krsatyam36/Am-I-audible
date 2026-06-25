"""Virtual-sink routing for gapless dual-track capture.

Topology created by :meth:`AudioRouter.setup` (mic and system kept separate):

    physical mic ──loopback──▶ [null sink am_i_audible_mic] ─▶ .monitor ─▶ record "mic"
    default out  ──loopback──▶ [null sink am_i_audible_sys] ─▶ .monitor ─▶ record "system"

The application records from the two null-sink **monitors**, which never change.
Swapping the physical microphone (:meth:`swap_mic`) only re-points the upstream
mic loopback, so the recording stream and timeline stay intact -- the basis of
gapless hot-swap.

Robust cleanup: use as a context manager (``with AudioRouter() as r:``). It also
registers ``atexit`` + SIGINT/SIGTERM handlers so the audio graph is never left
with stray ``am_i_audible_*`` objects, even on Ctrl-C or an unexpected exit.
"""

from __future__ import annotations

import atexit
import logging
import signal
from dataclasses import dataclass

from am_i_audible import config
from am_i_audible.audio.backends import Handle, RoutingError, detect_backend

log = logging.getLogger(__name__)


@dataclass
class Routes:
    """The two stable monitor sources the recorder should capture from."""

    mic_monitor: str
    system_monitor: str


class AudioRouter:
    def __init__(self, backend=None):
        self._backend = backend or detect_backend()
        # Created objects, in creation order; torn down in reverse.
        self._handles: list[Handle] = []
        # The current upstream mic loopback, tracked separately for hot-swap.
        self._mic_route: Handle | None = None
        self._current_mic_source: str | None = None
        self._active = False
        self._cleanup_registered = False

    # -- lifecycle --------------------------------------------------------- #
    def setup(self) -> Routes:
        """Create both null sinks and route default mic + system audio in."""
        if self._active:
            raise RoutingError("router already set up")

        self._register_cleanup()
        try:
            # 1. Two independent null sinks (dual-track).
            self._track(self._backend.create_null_sink(
                config.SINK_MIC, config.SINK_MIC_DESCRIPTION))
            self._track(self._backend.create_null_sink(
                config.SINK_SYSTEM, config.SINK_SYSTEM_DESCRIPTION))

            # 2. System audio: default output's monitor -> system sink.
            system_source = self._backend.monitor_of(self._backend.default_sink())
            self._track(self._backend.route(system_source, config.SINK_SYSTEM))

            # 3. Microphone: default source -> mic sink (tracked for hot-swap).
            mic_source = self._backend.default_source()
            self._mic_route = self._backend.route(mic_source, config.SINK_MIC)
            self._track(self._mic_route)
            self._current_mic_source = mic_source

            self._active = True
            log.info("router active (backend=%s, mic=%s, system=%s)",
                     self._backend.name, mic_source, system_source)
        except Exception:
            # Never leave a half-built graph behind.
            self.teardown()
            raise

        return Routes(
            mic_monitor=self._backend.monitor_of(config.SINK_MIC),
            system_monitor=self._backend.monitor_of(config.SINK_SYSTEM),
        )

    def swap_mic(self, new_source: str) -> None:
        """Re-point the mic loopback to ``new_source`` without breaking capture.

        The new loopback is created *before* the old one is destroyed so the mic
        sink keeps receiving audio across the swap -- the monitor the recorder
        reads from is unaffected, so no transcription data is lost.
        """
        if not self._active:
            raise RoutingError("router not set up")
        if new_source == self._current_mic_source:
            log.info("swap_mic: already on %s, nothing to do", new_source)
            return

        old_route = self._mic_route
        new_route = self._backend.route(new_source, config.SINK_MIC)  # overlap
        self._track(new_route)
        self._mic_route = new_route
        self._current_mic_source = new_source

        if old_route is not None:
            self._backend.destroy(old_route)
            if old_route in self._handles:
                self._handles.remove(old_route)
        log.info("swap_mic: now capturing from %s", new_source)

    def teardown(self) -> None:
        """Destroy every created object in reverse order. Idempotent."""
        while self._handles:
            handle = self._handles.pop()
            try:
                self._backend.destroy(handle)
            except Exception as exc:  # best-effort: keep tearing down the rest
                log.warning("teardown: failed to destroy %s: %s", handle.label, exc)
        self._mic_route = None
        self._current_mic_source = None
        self._active = False

    # -- helpers ----------------------------------------------------------- #
    @property
    def backend_name(self) -> str:
        return self._backend.name

    def capture_argv(self, target: str) -> list[str]:
        """Backend-specific argv that records ``target`` as raw s16 mono PCM
        on stdout. The recorder uses this so each backend captures its monitor
        with a tool that can actually resolve the monitor source name."""
        return self._backend.capture_argv(target)

    @property
    def current_mic(self) -> str | None:
        return self._current_mic_source

    def list_microphones(self) -> list[str]:
        """Candidate mic sources for hot-swap (excludes our own objects)."""
        return [s for s in self._backend.list_sources()
                if not s.startswith(config.OBJECT_PREFIX)]

    def _track(self, handle: Handle) -> Handle:
        self._handles.append(handle)
        return handle

    def _register_cleanup(self) -> None:
        if self._cleanup_registered:
            return
        atexit.register(self.teardown)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                prev = signal.getsignal(sig)

                def handler(signum, frame, _prev=prev):
                    self.teardown()
                    # chain to any previously installed handler / default
                    if callable(_prev):
                        _prev(signum, frame)
                    else:
                        raise KeyboardInterrupt

                signal.signal(sig, handler)
            except (ValueError, OSError):
                # not on the main thread -- atexit still covers us.
                pass
        self._cleanup_registered = True

    # -- context manager --------------------------------------------------- #
    def __enter__(self) -> "AudioRouter":
        self.setup()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.teardown()


def _demo() -> None:
    """Manual test harness: set up routing, print the monitors, wait, tear down.

    Run with:  python -m am_i_audible.audio.router
    Then in another terminal verify with `wpctl status` and `pw-record`.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    router = AudioRouter()
    routes = router.setup()
    print(f"\nBackend : {router.backend_name}")
    print(f"Mic monitor    : {routes.mic_monitor}")
    print(f"System monitor : {routes.system_monitor}")
    print("\nRouting is live. Verify in another terminal, e.g.:")
    print(f"  pw-record --target {routes.system_monitor} /tmp/sys.wav")
    print(f"  pw-record --target {routes.mic_monitor} /tmp/mic.wav")
    try:
        input("\nPress Enter to tear down... ")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        router.teardown()
        print("Torn down.")


if __name__ == "__main__":
    _demo()
