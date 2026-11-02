"""Turns a sanitized session into a clip.

Fails closed: the output path receives a file only after both leak gates pass. The gates report
counts and frame times, never the text they found.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .sanitize import Sanitized, sanitize_file
from .scrub import Scrubber

REPLAY_SHA = "6b50702e2e7305635f7feebed014eff0879f195a"
FORMATS = {"vertical": 1080, "landscape": 1920, "square": 1080}
DEFAULT_SPEED = 0.5  # half speed is the slowest pace that still shows a whole session of text
SPEED_RANGE = (0.25, 8.0)
MAX_BEATS = 9
SHEET_FRAMES = 24


class NotReady(Exception):
    """Something the pipeline needs is missing, so it refuses to run."""


class LeakFound(Exception):
    """A gate found private text. Holds categories and counts, never values."""

    def __init__(self, gate: str, counts: dict, detail: str = ""):
        self.gate, self.counts, self.detail = gate, dict(counts), detail
        found = ", ".join(f"{name}={count}" for name, count in sorted(self.counts.items()))
        super().__init__(f"{gate} gate: {found}{detail}")


def data_dir() -> Path:
    return Path(os.environ.get("FILMARE_DATA", "~/.local/share/filmare")).expanduser()


def renderer_dir() -> Path:
    return data_dir() / "agent-replay"


def replay(*args: str) -> str:
    """Runs the pinned renderer with a HOME of its own, so it can read no real session or credential."""
    bun = shutil.which("bun")
    if not bun or not (renderer_dir() / "src/cli.ts").exists():
        raise NotReady("The renderer is not installed. Run ./setup.sh")
    home = data_dir() / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "LANG": "en_US.UTF-8", "TMPDIR": tempfile.gettempdir()}
    done = subprocess.run([bun, "src/cli.ts", *args], cwd=renderer_dir(), env=env, capture_output=True, text=True)
    if done.returncode:
        # Its input is sanitized, so its error text is too. Only the end of it matters.
        raise RuntimeError("The renderer failed:\n" + "\n".join(done.stderr.splitlines()[-5:]))
    return done.stdout


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def ocr(frames: list[Path]) -> list[str]:
    """Returns the text read in each frame, in order."""
    tool = data_dir() / "bin/ocr"
    if not tool.exists():
        raise NotReady("The OCR helper is not installed, so the video cannot be checked. Run ./setup.sh")
    texts = [""] * len(frames)
    for start in range(0, len(frames), 20):
        batch = frames[start:start + 20]
        done = subprocess.run([str(tool), *map(str, batch)], capture_output=True, text=True, check=True)
        for line in done.stdout.splitlines():
            index, _, text = line.partition("\t")
            texts[start + int(index)] += text + "\n"
    return texts


def draw_label(text: str, width: int, out: Path) -> None:
    tool = data_dir() / "bin/label"
    if not tool.exists():
        raise NotReady("The label helper is not installed. Run ./setup.sh")
    subprocess.run([str(tool), text, str(width), str(out)], check=True)


def wait_for_machine() -> None:
    """A render peaks near 2.2 GB. Where the Teyla guard exists, it decides when one may start."""
    if shutil.which("teyla"):
        subprocess.run(["teyla", "load", "--wait", "--kind", "lane"], check=True, capture_output=True)


# --- choosing the beats -------------------------------------------------------------------------

STEP = re.compile(r"^\s*(\d+) (.) (\S+)\s*(.*)$")
PASSED = re.compile(r"→ .*?(\d{1,5}) passed")
FAILED = re.compile(r"→ .*?(\d{1,5}) failed")
SAY_RULES = (
    (re.compile(r"\bmerged\b", re.I), "Merged"),
    (re.compile(r"\bfix(ed|es)?\b", re.I), "The fix"),
    (re.compile(r"\bfound\b|\broot cause\b", re.I), "What it found"),
    (re.compile(r"\b(tests?|gate) (pass|passes|passed|green)\b|\bpass(ed|es)?\b", re.I), "It passes"),
    (re.compile(r"\bfail(ed|s|ure)?\b", re.I), "It fails"),
)


@dataclass
class Step:
    number: int
    failed: bool
    kind: str
    text: str


def parse_outline(text: str) -> list[Step]:
    steps = []
    for line in text.splitlines():
        match = STEP.match(line)
        if match:
            steps.append(Step(int(match[1]), match[2] == "✗", match[3], match[4]))
    return steps


def caption_for(step: Step) -> str | None:
    # Captions come from this fixed list plus a count, so they cannot carry session text.
    if step.kind == "shell":
        if failed := FAILED.search(step.text):
            return f"{failed[1]} failed"
        if passed := PASSED.search(step.text):
            return f"{passed[1]} tests pass"
        if "BUILD SUCCEEDED" in step.text:
            return "The build succeeds"
        if "BUILD FAILED" in step.text:
            return "The build fails"
        return "A command fails" if step.failed else None
    if step.kind == "say":
        for pattern, caption in SAY_RULES:
            if pattern.search(step.text):
                return caption
    return None


def choose_beats(steps: list[Step], limit: int = MAX_BEATS) -> list[str]:
    """Picks --cut values by rule: the first request, steps with an outcome, and the last message."""
    first = next((s for s in steps if s.kind == "user"), None)
    last = next((s for s in reversed(steps) if s.kind == "say"), None)
    middle: list[tuple[int, str]] = []
    for step in steps:
        caption = caption_for(step)
        ends = (first and step.number == first.number) or (last and step.number == last.number)
        if caption and not ends and (not middle or middle[-1][1] != caption):
            middle.append((step.number, caption))
    room = max(limit - 2, 0)
    if len(middle) > room:
        # Spread the picks over the session rather than taking only its opening.
        middle = [middle[i * len(middle) // room] for i in range(room)] if room else []
    beats = ([(first.number, "The ask")] if first else []) + middle
    if last and (not first or last.number != first.number):
        beats.append((last.number, "Where it ended"))
    return [f"{number}: {caption}" for number, caption in beats]


# --- the gates ----------------------------------------------------------------------------------


def layout_text(layout: dict) -> str:
    """Every string the renderer will draw, title card and captions included.

    Colour segments are joined first, so a phrase split across colours is still seen.
    """
    found = []

    def walk(node):
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk({key: value for key, value in layout.items() if key != "lines"})
    for line in layout.get("lines", []):
        found.append("".join(str(segment[0]) for segment in line.get("s", []) if segment))
    return "\n".join(found)


def check_text(sanitized: Sanitized) -> None:
    found = sanitized.scan()
    if found:
        raise LeakFound("text", found)


def check_layout(layout: dict, scrubber: Scrubber) -> None:
    found = scrubber.scan(layout_text(layout))
    if found:
        raise LeakFound("layout", found)


def check_frames(texts: list[str], scrubber: Scrubber) -> None:
    total: dict = {}
    seconds = []
    for second, text in enumerate(texts):
        found = scrubber.scan(text)
        if found:
            seconds.append(second)
            for name, count in found.items():
                total[name] = total.get(name, 0) + count
    if total:
        raise LeakFound("video", total, f" at seconds {seconds[:10]}")


# --- the pipeline -------------------------------------------------------------------------------


def label_text(started: str) -> str:
    try:
        day = datetime.fromisoformat(started.replace("Z", "+00:00")).strftime("%-d %b %Y")
    except ValueError:
        day = "date unknown"
    return f"Session replay · {day} · rendered from logs"


def outline_of(sanitized: Sanitized, work: Path) -> str:
    source = work / "session.jsonl"
    source.write_text(sanitized.jsonl, encoding="utf-8")
    return replay("outline", str(source))


def video_seconds(path: Path) -> float:
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(done.stdout.strip())


def render_clip(
    session: str | Path, out: Path, config: Config, *, fmt: str = "vertical", cuts: list[str] | None = None,
    duration: float | None = None, title: str | None = None,
) -> dict:
    if config.is_empty():
        raise NotReady("The config names nothing private, so a leak could not be recognised. Edit it first.")
    sanitized = sanitize_file(session, config)
    check_text(sanitized)
    base = data_dir() / "work"
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=base))  # mkdtemp makes it private to this user
    try:
        steps = parse_outline(outline_of(sanitized, work))
        chosen = cuts if cuts else choose_beats(steps)
        options = ["--format", fmt, "--title", title or sanitized.title or "Session replay"]
        for cut in chosen:
            options += ["--cut", cut]
        source = str(work / "session.jsonl")

        layout_path = work / "layout.json"
        replay(source, "--json", "-o", str(layout_path), *options)
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        check_layout(layout, sanitized.scrubber)

        natural = layout["durationInFrames"] / layout["fps"]
        speed = natural / duration if duration else DEFAULT_SPEED
        speed = max(SPEED_RANGE[0], min(SPEED_RANGE[1], speed))

        wait_for_machine()
        raw = work / "raw.mp4"
        replay(source, "-o", str(raw), "--speed", f"{speed:.3f}", *options)

        label = work / "label.png"
        draw_label(label_text(sanitized.started), FORMATS[fmt], label)
        labelled = work / "labelled.mp4"
        ffmpeg("-i", str(raw), "-i", str(label),
               "-filter_complex", "[0:v][1:v]overlay=(W-w)/2:24[v]", "-map", "[v]", "-map", "0:a?",
               "-map_metadata", "-1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-c:a", "copy",
               str(labelled))

        frames_dir = work / "frames"
        frames_dir.mkdir()
        ffmpeg("-i", str(labelled), "-vf", "fps=1", str(frames_dir / "%05d.png"))
        frames = sorted(frames_dir.glob("*.png"))
        if not frames:
            raise NotReady("No frames came out of the video, so it cannot be checked.")
        check_frames(ocr(frames), sanitized.scrubber)

        # Both gates passed. Only now does anything reach the path the user asked for.
        seconds = video_seconds(labelled)
        sheet = work / "sheet.png"
        ffmpeg("-i", str(labelled), "-frames:v", "1",
               "-vf", f"fps={SHEET_FRAMES / max(seconds, 1):.4f},scale=270:-1,tile=6x4:padding=6", str(sheet))
        out = out.expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        sheet_out = out.with_suffix(".sheet.png")
        shutil.move(str(sheet), sheet_out)
        shutil.move(str(labelled), out)
        with open(data_dir() / "renders.log", "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d')}\t{sanitized.agent}\t{seconds:.0f}\n")
        return {"video": out, "sheet": sheet_out, "seconds": seconds, "speed": speed, "beats": len(chosen), "frames": len(frames)}
    finally:
        shutil.rmtree(work, ignore_errors=True)
