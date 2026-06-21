"""Stateful capture controller driving the web UI.

Wraps :class:`AudioRouter` + :class:`DualTrackRecorder` (+ optional
:class:`TranscriptionEngine`) behind a thread-safe, call-from-anywhere API:

    start(...)        begin a session (optionally transcribing)
    pause()/resume()  hold/continue without ending the session
    swap_mic(target)  gapless mic device switch
    set_gain(...)     per-track software gain
    add_marker(label) drop a timestamped bookmark
    stop(name)        finalize: WAVs + transcript.md + markers.json, rename folder
    status()/telemetry()  state for the UI (telemetry also streams transcript deltas)
    list_sessions()/transcribe_file(...)  session history + re-transcription
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from am_i_audible import config
from am_i_audible.audio import diarize, engines, transcribe
from am_i_audible.audio.recorder import DualTrackRecorder
from am_i_audible.audio.router import AudioRouter
from am_i_audible.audio.transcribe import TranscriptionEngine

log = logging.getLogger(__name__)

# Live per-track speaker labels (diarization-lite from dual-track capture).
_TRACK_SPEAKER = {"mic": "You", "system": "Others"}


def _speaker_label(track_name: str) -> str:
    return _TRACK_SPEAKER.get(track_name, track_name.capitalize())


def _free_gpu() -> None:
    """Release GPU memory held by a dropped model (best-effort)."""
    try:
        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _notify(title: str, body: str) -> None:
    """Fire a Linux desktop notification (best-effort)."""
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", "am-I-audible", title, body],
                           check=False, capture_output=True)
        except Exception:
            pass


class CaptureController:
    def __init__(self):
        self._lock = threading.RLock()
        self._router: AudioRouter | None = None
        self._recorder: DualTrackRecorder | None = None
        self._stt: TranscriptionEngine | None = None
        self._out_dir: Path | None = None
        self._state = "idle"  # idle | recording | paused
        self._mode = "live"   # live | later
        self._markers: list[dict] = []
        self._pending_segments: list[dict] = []
        self.settings = {
            "transcribe": transcribe.available(),
            "engine": "whisper",
            "model": config.STT_MODEL,
            "language": config.STT_LANGUAGE or "",
            "window": config.STT_WINDOW_SECONDS,
            "diarize": False,
            "finalizeRepass": True,  # whole-file accurate re-pass on exit
            "sttAvailable": transcribe.available(),
            "diarizeAvailable": diarize.available(),
            "engines": engines.list_engines(),
        }

    def _refresh_engines(self) -> None:
        self.settings["engines"] = engines.list_engines()

    # -- queries ----------------------------------------------------------- #
    @property
    def is_recording(self) -> bool:
        return self._state in ("recording", "paused")

    def status(self) -> dict:
        with self._lock:
            mics, current_mic = [], None
            if self._router:
                try:
                    mics = self._router.list_microphones()
                    current_mic = self._router.current_mic
                except Exception:
                    pass
            gains = {t.name: t.gain for t in self._recorder.tracks} if self._recorder else {}
            return {
                "state": self._state,
                "backend": self._router.backend_name if self._router else None,
                "currentMic": current_mic,
                "microphones": mics,
                "seconds": self._recorder.seconds if self._recorder else 0.0,
                "outDir": str(self._out_dir) if self._out_dir else None,
                "tracks": [t.name for t in self._recorder.tracks] if self._recorder else [],
                "gains": gains,
                "markers": self._markers,
                "settings": self.settings,
                "sttDevice": self._stt_device(),
                "sttError": self._stt_error(),
            }

    def _stt_device(self) -> str | None:
        return self._stt.active_device if self._stt else None

    def _stt_error(self) -> str | None:
        return self._stt.error if self._stt else None

    def telemetry(self) -> dict:
        with self._lock:
            new_segments = self._pending_segments
            self._pending_segments = []
            if not self._recorder:
                return {"state": self._state, "seconds": 0.0, "tracks": {},
                        "segments": new_segments}
            return {
                "state": self._state,
                "seconds": self._recorder.seconds,
                "tracks": {
                    t.name: {"level": t.level, "peak": t.peak, "env": t.drain_envelope()}
                    for t in self._recorder.tracks
                },
                "segments": new_segments,
            }

    # -- commands ---------------------------------------------------------- #
    def start(self, *, record_mic: bool = True, record_system: bool = True,
              label: str | None = None, mode: str = "live") -> dict:
        with self._lock:
            if self.is_recording:
                return self.status()
            self._mode = "later" if mode == "later" else "live"
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            self._out_dir = config.RECORDINGS_ROOT / (f"{stamp}_{label}" if label else stamp)
            self._markers = []
            self._pending_segments = []
            self._router = AudioRouter()
            routes = self._router.setup()
            self._recorder = DualTrackRecorder(
                mic_monitor=routes.mic_monitor, system_monitor=routes.system_monitor,
                out_dir=self._out_dir, record_mic=record_mic, record_system=record_system)

            # Start capture FIRST so recording/waveforms/timer never wait on the
            # model. STT (one shared model for all tracks) loads in the background
            # and attaches its tap only once ready.
            self._stt = None
            self._recorder.start()
            self._state = "recording"

            # LIVE mode runs a fast, small model for a snappy preview; the accurate
            # whole-file transcript is produced in the background on stop. RECORD-ONLY
            # ("later") skips live STT entirely and transcribes after stop.
            if self._mode == "live" and transcribe.available(self.settings["engine"]):
                labels = {t.name: _speaker_label(t.name) for t in self._recorder.tracks}
                self._stt = TranscriptionEngine(
                    labels=labels, on_segment=self._on_segment,
                    model_size=config.STT_LIVE_MODEL,
                    language=self.settings["language"] or None,
                    window_seconds=config.STT_LIVE_WINDOW_SECONDS,
                    engine=self.settings["engine"])
                threading.Thread(target=self._load_stt, name="stt-load",
                                 daemon=True).start()

            log.info("controller: recording (%s) -> %s", self._mode, self._out_dir)
            return self.status()

    def _load_stt(self) -> None:
        eng = self._stt
        if eng is None:
            return
        if eng.start() and self._recorder is not None and self._stt is eng:
            self._recorder.set_tap(eng.feed)  # capture was already running
            log.info("controller: STT attached (device=%s)", eng.active_device)

    def pause(self) -> dict:
        with self._lock:
            if self._state == "recording" and self._recorder:
                self._recorder.set_paused(True)
                self._state = "paused"
            return self.status()

    def resume(self) -> dict:
        with self._lock:
            if self._state == "paused" and self._recorder:
                self._recorder.set_paused(False)
                self._state = "recording"
            return self.status()

    def swap_mic(self, target: str) -> dict:
        with self._lock:
            if self._router and self.is_recording:
                self._router.swap_mic(target)
            return self.status()

    def set_gain(self, name: str, value: float) -> dict:
        with self._lock:
            if self._recorder:
                self._recorder.set_gain(name, value)
            return self.status()

    def add_marker(self, label: str | None = None) -> dict:
        with self._lock:
            if self._recorder and self.is_recording:
                self._markers.append({
                    "t": round(self._recorder.seconds, 2),
                    "label": (label or f"Marker {len(self._markers) + 1}").strip(),
                })
            return self.status()

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            for k in ("transcribe", "engine", "model", "language", "window",
                      "diarize", "finalizeRepass"):
                if k in patch:
                    self.settings[k] = patch[k]
            self._refresh_engines()
            return self.status()

    def stop(self, save_name: str | None = None) -> dict:
        with self._lock:
            if not self.is_recording:
                return {"saved": None, "transcript": None, **self.status()}
            if self._recorder:
                self._recorder.set_tap(None)
                self._recorder.stop()
            if self._router:
                self._router.teardown()
            live_segments = self._stt.stop() if self._stt else []
            live_segments.sort(key=lambda s: s["start"])
            self._stt = None
            saved_dir = self._finalize_name(save_name)
            # FAST PATH: save the live transcript + markers immediately and return.
            # The high-accuracy whole-file re-pass runs in the background so Stop
            # never freezes the UI (it used to block for the whole re-transcription).
            transcript_path = self._save_artifacts(saved_dir, live_segments)
            mode = self._mode
            # Background transcription: always for record-only ("later"); for live,
            # the accurate whole-file re-pass when finalizeRepass is on.
            do_bg = bool(saved_dir and transcribe.available(self.settings["engine"])
                         and (mode == "later" or self.settings.get("finalizeRepass")))
            repass = None
            if do_bg:
                repass = (saved_dir, self.settings["model"],
                          self.settings["language"] or None, self.settings["engine"],
                          bool(self.settings.get("diarize")), mode == "later")
            self._state = "idle"
            self._recorder = self._router = None
            self._out_dir = None
            out = {"saved": str(saved_dir) if saved_dir else None,
                   "transcript": str(transcript_path) if transcript_path else None,
                   "transcribing": do_bg, "mode": mode}
            status = self.status()

        _free_gpu()  # release the live model before the bg re-pass loads its own
        if repass:
            threading.Thread(target=self._finalize_bg, args=repass,
                             name="finalize", daemon=True).start()
        log.info("controller: stopped -> %s", out["saved"])
        return {**out, **status}

    def _finalize_bg(self, saved_dir: Path, model: str, language: str | None,
                     engine: str, do_diarize: bool, notify: bool = False) -> None:
        """Background whole-file transcription -> write the saved transcript."""
        try:
            wavs = sorted(saved_dir.glob("*.wav"))
            if not wavs:
                return
            labeled = {transcribe.SPEAKER_LABELS.get(w.name, w.stem): w for w in wavs}
            segs = transcribe.transcribe_labeled(
                labeled, model_size=model, language=language, engine=engine)
            if do_diarize and diarize.available():
                sys_wav = saved_dir / config.SYSTEM_TRACK_FILENAME
                if sys_wav.exists():
                    try:
                        turns = diarize.diarize_wav(sys_wav)
                        diarize.relabel([s for s in segs if s.get("speaker") == "Others"],
                                        turns, "Others")
                    except Exception as exc:
                        log.warning("diarization failed: %s", exc)
            if segs:
                md = transcribe.segments_to_markdown(segs, saved_dir.name)
                (saved_dir / "transcript.md").write_text(md)
                config.TRANSCRIPTS_ROOT.mkdir(parents=True, exist_ok=True)
                (config.TRANSCRIPTS_ROOT / f"{saved_dir.name}.md").write_text(md)
                log.info("background transcript: %d segments -> %s",
                         len(segs), saved_dir.name)
                if notify:
                    _notify("Transcript ready ✓",
                            f"{saved_dir.name} — {len(segs)} segments")
            elif notify:
                _notify("Transcription finished",
                        f"{saved_dir.name} — no speech detected")
        except Exception as exc:
            log.warning("background transcript failed: %s", exc)
            if notify:
                _notify("Transcription failed", saved_dir.name)
        finally:
            _free_gpu()

    # -- session history --------------------------------------------------- #
    def list_sessions(self) -> list[dict]:
        root = config.RECORDINGS_ROOT
        if not root.exists():
            return []
        sessions = []
        for d in sorted(root.iterdir(), reverse=True):
            if not d.is_dir() or d.name == config.TRANSCRIPTS_ROOT.name:
                continue
            wavs = sorted(d.glob("*.wav"))
            if not wavs:
                continue
            size = sum(w.stat().st_size for w in wavs)
            dur = max((w.stat().st_size / (config.BYTES_PER_FRAME * config.SAMPLE_RATE)
                       for w in wavs), default=0.0)
            sessions.append({
                "name": d.name,
                "tracks": [w.name for w in wavs],
                "sizeBytes": size,
                "seconds": round(dur, 1),
                "hasTranscript": (d / "transcript.md").exists(),
                "mtime": int(d.stat().st_mtime),
            })
        return sessions

    def transcribe_file(self, name: str) -> dict:
        """Re-transcribe a past session with speaker labels (You / Others).

        Blocking; the server runs it off-thread.
        """
        session = config.RECORDINGS_ROOT / name
        wavs = sorted(session.glob("*.wav"))
        if not wavs:
            return {"ok": False, "error": "no audio in session"}
        if not transcribe.available():
            return {"ok": False, "error": "faster-whisper not installed"}
        labeled = {transcribe.SPEAKER_LABELS.get(w.name, w.stem): w for w in wavs}
        segs = transcribe.transcribe_labeled(
            labeled, model_size=self.settings["model"],
            language=self.settings["language"] or None,
            engine=self.settings["engine"])
        # optional: split the system track into individual speakers
        if self.settings.get("diarize") and diarize.available():
            sys_wav = session / config.SYSTEM_TRACK_FILENAME
            if sys_wav.exists():
                try:
                    turns = diarize.diarize_wav(sys_wav)
                    diarize.relabel([s for s in segs if s.get("speaker") == "Others"],
                                    turns, "Others")
                except Exception as exc:
                    log.warning("diarization failed: %s", exc)
        self._markers = []  # re-run isn't tied to live markers
        path = self._save_artifacts(session, segs)
        return {"ok": True, "segments": len(segs), "transcript": str(path)}

    # -- helpers ----------------------------------------------------------- #
    def _on_segment(self, item: dict) -> None:
        with self._lock:
            self._pending_segments.append(item)

    def _finalize_name(self, save_name: str | None) -> Path | None:
        if not self._out_dir or not self._out_dir.exists() or not save_name:
            return self._out_dir
        safe = "".join(c for c in save_name if c.isalnum() or c in " -_.").strip()
        safe = safe.replace(" ", "_")
        if not safe:
            return self._out_dir
        target = self._out_dir.parent / safe
        if target.exists():
            target = self._out_dir.parent / f"{safe}_{self._out_dir.name}"
        self._out_dir.rename(target)
        return target

    def _save_artifacts(self, session_dir: Path | None, segments: list[dict]) -> Path | None:
        if not session_dir or not session_dir.exists():
            return None
        if self._markers:
            (session_dir / "markers.json").write_text(json.dumps(self._markers, indent=2))
        if not segments:
            return None
        md = transcribe.segments_to_markdown(segments, session_dir.name)
        (session_dir / "transcript.md").write_text(md)
        config.TRANSCRIPTS_ROOT.mkdir(parents=True, exist_ok=True)
        mirror = config.TRANSCRIPTS_ROOT / f"{session_dir.name}.md"
        mirror.write_text(md)
        return mirror
