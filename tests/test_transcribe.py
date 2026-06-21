"""Unit tests for transcription helpers that don't require a model download."""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from am_i_audible.audio import transcribe  # noqa: E402
from am_i_audible.core.controller import _speaker_label  # noqa: E402


class TranscribeHelperTests(unittest.TestCase):
    def test_downsample_48k_to_16k_ratio(self):
        x = np.ones(48_000, dtype=np.float32)
        out = transcribe._to_16k(x)
        self.assertEqual(out.size, 16_000)  # exactly 1/3

    def test_downsample_handles_short_input(self):
        # must not crash on tiny input; returns a float32 array (size impl-defined)
        out = transcribe._to_16k(np.zeros(2, dtype=np.float32))
        self.assertEqual(out.dtype, np.float32)
        self.assertLessEqual(out.size, 2)

    def test_markdown_includes_speaker_labels(self):
        segs = [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "You"},
                {"start": 1.0, "end": 2.0, "text": "hello", "speaker": "Others"}]
        md = transcribe.segments_to_markdown(segs, "meeting")
        self.assertIn("# meeting", md)
        self.assertIn("**You**", md)
        self.assertIn("**Others**", md)
        self.assertIn("[00:00:00]", md)

    def test_speaker_label_mapping(self):
        self.assertEqual(_speaker_label("mic"), "You")
        self.assertEqual(_speaker_label("system"), "Others")
        self.assertEqual(_speaker_label("aux"), "Aux")

    def test_speaker_filename_labels(self):
        self.assertEqual(transcribe.SPEAKER_LABELS["mic.wav"], "You")
        self.assertEqual(transcribe.SPEAKER_LABELS["system.wav"], "Others")


if __name__ == "__main__":
    unittest.main()
