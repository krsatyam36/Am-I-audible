"""Standalone background transcription worker.

Run as a **detached** process so it survives the `listen` server exiting (e.g.
after "Exit & save" in record-only mode). It transcribes a session's WAV tracks
with speaker labels, writes ``transcript.md`` (+ the mirror in
``generated_transcripts/``), and fires a desktop notification when done.

    python -m am_i_audible.jobs --dir <session_dir> [--model M --language L \
        --engine E --diarize --notify]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from am_i_audible import config
from am_i_audible.audio import diarize, transcribe


def spawn(saved_dir, model=None, language=None, engine="whisper",
          diarize=False, notify=False) -> None:
    """Launch the transcription worker as a DETACHED process.

    start_new_session=True puts it in its own session so it keeps running after
    the `listen` server exits (the whole point of record-only + Exit & save).
    """
    import os
    import am_i_audible
    src_root = str(Path(am_i_audible.__file__).resolve().parents[1])
    env = {**os.environ,
           "PYTHONPATH": src_root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    cmd = [sys.executable, "-m", "am_i_audible.jobs", "--dir", str(saved_dir),
           "--engine", engine]
    if model:
        cmd += ["--model", model]
    if language:
        cmd += ["--language", language]
    if diarize:
        cmd += ["--diarize"]
    if notify:
        cmd += ["--notify"]
    subprocess.Popen(cmd, env=env, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", "am-I-audible", title, body],
                           check=False, capture_output=True)
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="am_i_audible.jobs")
    p.add_argument("--dir", required=True)
    p.add_argument("--model", default=None)
    p.add_argument("--language", default=None)
    p.add_argument("--engine", default="whisper")
    p.add_argument("--diarize", action="store_true")
    p.add_argument("--notify", action="store_true")
    a = p.parse_args(argv)

    d = Path(a.dir)
    wavs = sorted(d.glob("*.wav"))
    if not wavs:
        if a.notify:
            _notify("Transcription skipped", f"{d.name} — no audio")
        return 1
    if not transcribe.available(a.engine):
        if a.notify:
            _notify("Transcription unavailable", "faster-whisper not installed")
        return 1
    try:
        labeled = {transcribe.SPEAKER_LABELS.get(w.name, w.stem): w for w in wavs}
        try:
            segs = transcribe.transcribe_labeled(
                labeled, model_size=a.model, language=a.language or None, engine=a.engine)
        except Exception:
            # transient GPU OOM/contention on long files -> retry on CPU (slow but
            # always completes, so a record-only transcript is never lost).
            import traceback
            traceback.print_exc()
            print("GPU transcription failed; retrying on CPU…", file=sys.stderr)
            segs = transcribe.transcribe_labeled(
                labeled, model_size=a.model, language=a.language or None,
                engine=a.engine, device="cpu")
        if a.diarize and diarize.available():
            sys_wav = d / config.SYSTEM_TRACK_FILENAME
            if sys_wav.exists():
                try:
                    diarize.relabel([s for s in segs if s.get("speaker") == "Others"],
                                    diarize.diarize_wav(sys_wav), "Others")
                except Exception:
                    pass
        md = transcribe.segments_to_markdown(segs, d.name)
        (d / "transcript.md").write_text(md)
        config.TRANSCRIPTS_ROOT.mkdir(parents=True, exist_ok=True)
        (config.TRANSCRIPTS_ROOT / f"{d.name}.md").write_text(md)
        if a.notify:
            _notify("Transcript ready ✓", f"{d.name} — {len(segs)} segments")
        return 0
    except Exception as exc:
        import traceback
        traceback.print_exc()  # visible in the job log, never silent
        if a.notify:
            _notify("Transcription failed", f"{d.name}: {str(exc)[:80]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
