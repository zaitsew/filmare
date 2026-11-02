"""Command line. Exit codes: 0 done, 1 failed, 2 not ready, 3 a leak gate refused the video."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import DEFAULT_PATH, Config, ConfigError
from .logs import parse_session
from .render import (FORMATS, REPLAY_SHA, LeakFound, NotReady, data_dir, outline_of, render_clip, renderer_dir)
from .sanitize import one_line, sanitize_file
from .scrub import Scrubber

TITLE_SIZE_LIMIT = 20 * 1024 * 1024  # reading a title means parsing the whole log


def session_files() -> list[Path]:
    home = Path.home()
    found = list((home / ".claude/projects").glob("*/*.jsonl"))
    found += list((home / ".codex/sessions").glob("*/*/*/rollout-*.jsonl"))
    found += list((home / ".codex/sessions").glob("*/*/*/rollout-*.jsonl.zst"))
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def find_session(ref: str) -> Path:
    path = Path(ref).expanduser()
    if path.is_file():
        return path
    matches = [p for p in session_files() if ref in p.name]
    if not matches:
        raise NotReady("No session matches that reference. Try: filmare list")
    if len(matches) > 1:
        raise NotReady(f"{len(matches)} sessions match that reference. Give more of the id.")
    return matches[0]


def cmd_doctor(_args) -> int:
    missing = []

    def need(ok: bool, what: str, fix: str) -> None:
        print(f"{'ok' if ok else 'MISSING':8}{what}" + ("" if ok else f"  → {fix}"))
        if not ok:
            missing.append(what)

    need(sys.version_info >= (3, 14), "Python 3.14 or newer (reads compressed Codex logs)", "brew install python@3.14")
    for tool in ("bun", "ffmpeg", "ffprobe"):
        need(bool(shutil.which(tool)), tool, f"install {tool}, then run ./setup.sh")
    pinned = False
    if (renderer_dir() / ".git").exists():
        head = subprocess.run(["git", "-C", str(renderer_dir()), "rev-parse", "HEAD"], capture_output=True, text=True)
        pinned = head.stdout.strip() == REPLAY_SHA
    need(pinned, "renderer at the pinned commit", "./setup.sh")
    need((renderer_dir() / "dist/player.js.txt").exists(), "renderer's player built", "./setup.sh")
    need((data_dir() / "bin/ocr").exists(), "OCR helper", "./setup.sh")
    need((data_dir() / "bin/label").exists(), "label helper", "./setup.sh")
    try:
        filled = not Config.load().is_empty()
        need(filled, f"config names something private: {DEFAULT_PATH}", "edit the file")
    except ConfigError as error:
        need(False, f"config: {error}", f"create {DEFAULT_PATH}")
    if missing:
        print(f"\n{len(missing)} missing.")
        return 2
    print("\nReady. The first render downloads Chrome Headless Shell, about 170 MB.")
    return 0


def cmd_list(args) -> int:
    scrub = Scrubber(*_names_and_private())
    for path in session_files()[: args.limit]:
        when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        size = path.stat().st_size
        title = "(large)"
        if size < TITLE_SIZE_LIMIT:
            try:
                session = parse_session(path)
                title = one_line(session.title or "", scrub, 60) or "(untitled)"
            except (ValueError, ImportError, OSError):
                title = "(unreadable)"
        agent = "codex" if path.name.startswith("rollout-") else "claude"
        print(f"{when}  {agent:6} {size / 1e6:6.1f} MB  {path.name.split('.')[0][-36:]}  {title}")
    return 0


def _names_and_private():
    config = Config.load()
    return config.names, config.private_strings()


def cmd_outline(args) -> int:
    sanitized = sanitize_file(find_session(args.session), Config.load())
    with tempfile.TemporaryDirectory() as folder:
        print(outline_of(sanitized, Path(folder)))
    return 0


def cmd_render(args) -> int:
    result = render_clip(
        find_session(args.session), Path(args.output), Config.load(), fmt=args.format, cuts=args.cut,
        duration=args.duration, title=args.title,
    )
    print(f"{result['video']}: {result['seconds']:.0f} s, {result['beats']} beats, speed {result['speed']:.2f}.")
    print(f"Both leak gates passed, {result['frames']} frames read.")
    print(f"Review it before publishing. Contact sheet: {result['sheet']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filmare", description="Turn AI coding sessions into safe, watchable video.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="say what is missing").set_defaults(run=cmd_doctor)
    listing = commands.add_parser("list", help="recent Claude Code and Codex sessions")
    listing.add_argument("--limit", type=int, default=20)
    listing.set_defaults(run=cmd_list)
    outline = commands.add_parser("outline", help="number a session's steps, sanitized, to pick --cut values")
    outline.add_argument("session", help="a session id, part of one, or a path")
    outline.set_defaults(run=cmd_outline)
    render = commands.add_parser("render", help="render a clip, refusing it if a leak gate finds anything")
    render.add_argument("session", help="a session id, part of one, or a path")
    render.add_argument("-o", "--output", required=True)
    render.add_argument("--format", choices=sorted(FORMATS), default="vertical")
    render.add_argument("--cut", action="append", help="steps and a caption, like '4-9: Finds the bug'. Default: chosen by rule")
    render.add_argument("--duration", type=float, help="target length in seconds; sets the speed")
    render.add_argument("--title", help="title card; default: the session's title")
    render.set_defaults(run=cmd_render)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except LeakFound as error:
        print(f"REFUSED by the {error}. The video was deleted. Nothing was written to the output path.", file=sys.stderr)
        return 3
    except (NotReady, ConfigError) as error:
        print(error, file=sys.stderr)
        return 2
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Failed: {error}", file=sys.stderr)
        return 1
