"""The pipeline's decisions, tested without the renderer. All data here is invented."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from filmare import render
from filmare.config import Config
from filmare.scrub import Scrubber

OUTLINE = """Claude Code · demo · "Fix" · 9 steps

turn 1
    1   user   Please fix the broken link
    2   read   src/a.swift
    3   say    I found the cause in the router
    4   edit   src/a.swift +3 -1
    5 ✗ shell  npm test  → 2 failed
    6   shell  npm test  → 37 passed
    7   shell  git status  → [3 lines hidden]
    8   say    Merged the change
    9   say    All done, nothing left
"""


def log_file(folder: str) -> Path:
    path = Path(folder) / "log.jsonl"
    path.write_text(json.dumps({
        "type": "user", "timestamp": "2026-11-01T10:00:00Z", "cwd": "/tmp/demo", "message": {"content": "Hello"},
    }) + "\n", encoding="utf-8")
    return path


class BeatTests(unittest.TestCase):
    def test_outline_is_parsed(self):
        steps = render.parse_outline(OUTLINE)
        self.assertEqual(len(steps), 9)
        self.assertTrue(steps[4].failed)
        self.assertEqual(steps[5].kind, "shell")

    def test_beats_follow_the_rules(self):
        beats = render.choose_beats(render.parse_outline(OUTLINE))
        self.assertEqual(beats, [
            "1: The ask", "3: What it found", "5: 2 failed", "6: 37 tests pass", "8: Merged", "9: Where it ended",
        ])

    def test_captions_carry_no_session_text(self):
        steps = [render.Step(1, False, "say", "Zelda fixed Quentin's booking")]
        self.assertEqual(render.caption_for(steps[0]), "The fix")

    def test_beats_respect_the_limit_and_keep_both_ends(self):
        lines = ["    1   user   Go"] + [f"{n:5}   shell  x  → {n} passed" for n in range(2, 40)] + ["   40   say    End"]
        beats = render.choose_beats(render.parse_outline("\n".join(lines)), limit=5)
        self.assertEqual(len(beats), 5)
        self.assertTrue(beats[0].startswith("1:"))
        self.assertTrue(beats[-1].startswith("40:"))

    def test_a_session_with_no_steps_gives_no_beats(self):
        self.assertEqual(render.choose_beats([]), [])


class GateTests(unittest.TestCase):
    def test_a_phrase_split_across_colour_segments_is_caught(self):
        layout = {"lines": [{"at": 0, "s": [["Avenida Inv", "a"], ["entada 7", "b"]]}]}
        with self.assertRaises(render.LeakFound) as caught:
            render.check_layout(layout, Scrubber(private={"Avenida Inventada 7"}))
        self.assertNotIn("Avenida", str(caught.exception))

    def test_a_title_outside_the_lines_is_caught(self):
        with self.assertRaises(render.LeakFound):
            render.check_layout({"title": {"text": "A trip with Zelda"}, "lines": []}, Scrubber(["Zelda"]))

    def test_a_phrase_wrapped_across_lines_is_caught(self):
        layout = {"lines": [{"s": [["Avenida Inven", "a"]]}, {"s": [["tada 7", "a"]]}]}
        with self.assertRaises(render.LeakFound):
            render.check_layout(layout, Scrubber(private={"Avenida Inventada 7"}))

    def test_frames_report_counts_and_seconds_not_text(self):
        with self.assertRaises(render.LeakFound) as caught:
            render.check_frames(["clean", "Zelda was here"], Scrubber(["Zelda"]))
        self.assertEqual(caught.exception.counts, {"name": 1})
        self.assertIn("[1]", str(caught.exception))
        self.assertNotIn("Zelda", str(caught.exception))

    def test_label_names_the_date_and_says_it_is_a_replay(self):
        self.assertEqual(render.label_text("2026-11-01T10:00:00Z"), "Session replay · 1 Nov 2026 · rendered from logs")
        self.assertIn("date unknown", render.label_text(""))


class PipelineTests(unittest.TestCase):
    """Replaces the external tools and checks where the pipeline refuses."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        patcher = mock.patch.dict("os.environ", {"FILMARE_DATA": str(self.root / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = Config(names=["Zelda"])
        self.out = self.root / "out/clip.mp4"
        self.layout = {"fps": 30, "width": 1080, "height": 1920, "durationInFrames": 300, "lines": [{"s": [["fine", "a"]]}]}

    def fake_replay(self, *args):
        if args[0] == "outline":
            return OUTLINE
        target = Path(args[args.index("-o") + 1])
        if "--json" in args:
            target.write_text(json.dumps(self.layout), encoding="utf-8")
        else:
            target.write_bytes(b"video")
        return ""

    @staticmethod
    def fake_ffmpeg(*args):
        target = Path(args[-1])
        if "%05d" in target.name:
            (target.parent / "00001.png").write_bytes(b"frame")
        else:
            target.write_bytes(b"video")

    def run_pipeline(self, ocr_text="clean", **options):
        with mock.patch.object(render, "replay", self.fake_replay), mock.patch.object(render, "ffmpeg", self.fake_ffmpeg), \
                mock.patch.object(render, "ocr", lambda frames: [ocr_text] * len(frames)), \
                mock.patch.object(render, "draw_label", lambda text, width, out: out.write_bytes(b"png")), \
                mock.patch.object(render, "wait_for_machine", lambda: None), \
                mock.patch.object(render, "video_seconds", lambda path: 20.0):
            return render.render_clip(log_file(str(self.root)), self.out, self.config, **options)

    def assert_nothing_left(self):
        self.assertFalse(self.out.exists())
        self.assertFalse(self.out.with_suffix(".sheet.png").exists())
        self.assertEqual(list((self.root / "data/work").iterdir()), [])

    def test_a_clean_run_writes_the_clip_and_the_log(self):
        result = self.run_pipeline()
        self.assertTrue(self.out.exists())
        self.assertTrue(self.out.with_suffix(".sheet.png").exists())
        self.assertEqual(result["speed"], render.DEFAULT_SPEED)
        self.assertEqual(len((self.root / "data/renders.log").read_text().splitlines()), 1)
        self.assertEqual(list((self.root / "data/work").iterdir()), [])

    def test_a_name_in_a_frame_blocks_the_output(self):
        with self.assertRaises(render.LeakFound):
            self.run_pipeline(ocr_text="Zelda")
        self.assert_nothing_left()

    def test_a_name_in_the_layout_blocks_before_rendering(self):
        self.layout["lines"] = [{"s": [["Zel", "a"], ["da", "b"]]}]
        with self.assertRaises(render.LeakFound):
            self.run_pipeline()
        self.assert_nothing_left()

    def test_a_private_title_blocks_the_output(self):
        original = self.fake_replay

        def echo_title(*args):
            if args[0] != "outline" and "--json" in args:
                self.layout["title"] = args[args.index("--title") + 1]
            return original(*args)

        with mock.patch.object(self, "fake_replay", echo_title):
            with self.assertRaises(render.LeakFound):
                self.run_pipeline(title="A trip with Zelda")
        self.assert_nothing_left()

    def test_an_empty_config_refuses_to_run(self):
        self.config = Config()
        with self.assertRaises(render.NotReady):
            self.run_pipeline()

    def test_missing_ocr_refuses_and_writes_nothing(self):
        def no_ocr(frames):
            raise render.NotReady("no OCR")

        with mock.patch.object(render, "replay", self.fake_replay), mock.patch.object(render, "ffmpeg", self.fake_ffmpeg), \
                mock.patch.object(render, "ocr", no_ocr), \
                mock.patch.object(render, "draw_label", lambda text, width, out: out.write_bytes(b"png")), \
                mock.patch.object(render, "wait_for_machine", lambda: None):
            with self.assertRaises(render.NotReady):
                render.render_clip(log_file(str(self.root)), self.out, self.config)
        self.assert_nothing_left()

    def test_duration_sets_the_speed_within_limits(self):
        self.assertEqual(self.run_pipeline(duration=20)["speed"], 0.5)
        self.assertEqual(self.run_pipeline(duration=100000)["speed"], render.SPEED_RANGE[0])
        self.assertEqual(self.run_pipeline(duration=0.1)["speed"], render.SPEED_RANGE[1])


if __name__ == "__main__":
    unittest.main()
