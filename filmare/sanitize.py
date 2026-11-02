"""Builds a log that is safe to render. Nothing passes unless a line here copies it.

Dropped outright: thinking, images, attachments, subagent work, MCP arguments and results,
and file contents. Commands are cut to one line. Command output shrinks to status lines.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass

from .config import Config
from .logs import Session, parse_session
from .scrub import Scrubber

CODE_SUFFIXES = (".swift", ".ts", ".tsx", ".js", ".jsx", ".kt", ".py", ".sh", ".rs", ".go", ".css", ".sql", ".yml", ".yaml")
PRIVATE_PATH = re.compile(r"(^|/)(data|fixtures|secrets?|memory)(/|$)|\.env|\.p8$|\.pem$|\.strings$|\.xcstrings$|\.json$")
# Output is never copied. A status is rebuilt from these numbers and words, so a line of a private
# file that happens to say "passed" cannot reach the video.
STATUS = re.compile(r"(?<![\w.-])(\d{1,5}) (passed|failed)\b|\bBUILD (SUCCEEDED|FAILED)\b|\bExecuted (\d{1,5}) tests?\b")
# Only a test or build run reports a status. Every step of the command must be one of these, so
# `cat clients.txt` or `npm test && cat clients.txt` cannot supply a number.
RUNNERS = {"check.sh", "vitest", "jest", "pytest", "xcodebuild", "xcb", "tsc"}
RUNNER_SUBCOMMANDS = {
    "swift": {"test", "build"}, "cargo": {"test", "build", "check"}, "deno": {"test", "check"}, "make": {"test", "check"},
    "npm": {"test"}, "pnpm": {"test"}, "yarn": {"test"}, "bun": {"test"},
}
GRADLE_TASK = re.compile(r"[\w:-]*(?:test|check|build|assemble)[\w:-]*", re.I)
QUIET_STEP = re.compile(r"cd\s+\S+|(?:export|unset)\s+\S+|set\s+[-+]\w+(?:\s+\w+)?|(?:tail|head)(?:\s+-\w+(?:\s+\d+)?)*")
HIDDEN_CODE = "+[code hidden]"


@dataclass
class Sanitized:
    agent: str
    title: str
    started: str
    jsonl: str
    scrubber: Scrubber

    def strings(self) -> str:
        """Every string the renderer will receive, for the leak scan."""
        found: list[str] = []

        def walk(node):
            if isinstance(node, str):
                found.append(node)
            elif isinstance(node, dict):
                for key, value in node.items():
                    found.append(str(key))  # a Codex edit is keyed by its file path
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        for line in self.jsonl.splitlines():
            walk(json.loads(line))
        return "\n".join(found)

    def scan(self) -> Counter:
        return self.scrubber.scan(self.strings())


def clip_text(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    stops = [m.end() for m in re.finditer(r"[.!?:](?=\s)", head)]
    return (head[: stops[-1]] if stops else head.rsplit(" ", 1)[0]).rstrip() + " …"


def is_runner(step: str) -> bool:
    words = step.split()
    while words and re.fullmatch(r"\w+=\S*", words[0]):
        words.pop(0)
    if words[:1] == ["npx"]:
        words.pop(0)
    if not words:
        return False
    program, args = words[0].rsplit("/", 1)[-1], words[1:]
    if program in RUNNERS:
        return True
    if program in ("npm", "pnpm", "yarn", "bun") and args[:1] == ["run"]:
        args = args[1:]
    if program in RUNNER_SUBCOMMANDS:
        return args[:1] != [] and args[0] in RUNNER_SUBCOMMANDS[program]
    if program in ("python", "python3"):
        return args[:2] in (["-m", "unittest"], ["-m", "pytest"])
    if program in ("gradle", "gradlew"):
        return args[:1] != [] and bool(GRADLE_TASK.fullmatch(args[0]))
    return False


def reports_status(command: str) -> bool:
    # Only these plain characters count. Quoting, expansion, redirects and odd spaces all hide the status.
    plain = re.sub(r"(?<=[ \t])2>&1(?=[ \t\n]|$)", " ", command)
    if not re.fullmatch(r"[A-Za-z0-9_./=:@+,%~ \t\n;&|-]*", plain) or re.search(r"(?<!&)&(?!&)", plain):
        return False
    steps = [step.strip() for step in re.split(r"&&|\|\||[;|\n]", plain) if step.strip()]
    return any(is_runner(s) for s in steps) and all(is_runner(s) or QUIET_STEP.fullmatch(s) for s in steps)


def status_of(output: str, ok: bool, command: str = "") -> str:
    lines = output.splitlines()
    status: dict[str, str] = {}
    for count, word, build, executed in STATUS.findall(output) if reports_status(command) else ():
        if word:
            status[word] = f"{count} {word}"
        elif build:
            status["build"] = f"BUILD {build}"
        else:
            status["executed"] = f"{executed} tests"
    if status:
        return " · ".join(status.values())
    count = sum(1 for line in lines if line.strip())
    if count:
        return f"[{count} lines hidden]"
    return "[no output]" if ok else "[failed]"


def shows_code(path: str, config: Config) -> bool:
    return config.show_code and path.endswith(CODE_SUFFIXES) and not PRIVATE_PATH.search(path)


def one_line(text: str, scrub: Scrubber, limit: int = 120) -> str:
    # Scrub before cutting: a cut can leave half a token, which no pattern recognises.
    lines = [line for line in scrub(text).splitlines() if line.strip()]
    return lines[0].strip()[:limit] if lines else ""


def clean_event(event: dict, scrub: Scrubber, config: Config) -> dict | None:
    kind, ok, at = event["kind"], event.get("ok", True), event.get("at", "")
    if kind in ("user", "say"):
        text = clip_text(scrub(event["text"]), config.say_limit)
        return {"kind": kind, "at": at, "text": text} if text else None
    if kind == "shell":
        return {"kind": kind, "at": at, "ok": ok, "command": one_line(event["command"], scrub),
                "output": status_of(event.get("output", ""), ok, event["command"])}
    if kind == "read":
        return {"kind": kind, "at": at, "ok": ok, "path": one_line(event["path"], scrub), "lines": int(event.get("lines") or 0)}
    if kind == "search":
        return {"kind": kind, "at": at, "ok": ok, "tool": event.get("tool", "Grep"),
                "query": one_line(event["query"], scrub, 60), "path": one_line(event.get("path", ""), scrub)}
    if kind == "edit":
        path = event["path"]
        clean = {"kind": kind, "at": at, "ok": ok, "path": one_line(path, scrub), "mode": event["mode"]}
        visible = shows_code(path, config)
        if "unified" in event:
            clean["unified"] = "\n".join(scrub(l) for l in event["unified"].split("\n")) if visible else ""
        else:
            clean["hunks"] = [
                {"oldStart": h.get("oldStart", 1), "newStart": h.get("newStart", 1), "lines": [scrub(l) for l in h.get("lines", [])]}
                for h in event.get("hunks", [])
            ] if visible else []
        return clean
    if kind == "tool":
        return {"kind": kind, "at": at, "ok": ok, "name": one_line(event["name"], scrub, 60)}
    return None


def write_claude(session: Session, events: list[dict], title: str, cwd: str) -> list[dict]:
    records: list[dict] = [{"aiTitle": title}] if title else []
    base = {"cwd": cwd, "version": session.version}

    def add(role: str, at: str, blocks: list[dict], result: dict | None = None) -> None:
        message = {"content": blocks, **({"model": session.model} if role == "assistant" and session.model else {})}
        record = {"type": role, "timestamp": at, **base, "message": message}
        if result is not None:
            record["toolUseResult"] = result
        records.append(record)

    for number, e in enumerate(events):
        if e["kind"] == "user":
            add("user", e["at"], [{"type": "text", "text": e["text"]}])
        elif e["kind"] == "say":
            add("assistant", e["at"], [{"type": "text", "text": e["text"]}])
        else:
            name, raw, body, result = _claude_call(e)
            call = f"call_{number}"
            add("assistant", e["at"], [{"type": "tool_use", "id": call, "name": name, "input": raw}])
            add("user", e["at"], [{"type": "tool_result", "tool_use_id": call, "content": body, "is_error": not e["ok"]}], result)
    return records


def _claude_call(e: dict) -> tuple[str, dict, str, dict]:
    if e["kind"] == "shell":
        return "Bash", {"command": e["command"]}, e["output"], {"stdout": e["output"]}
    if e["kind"] == "read":
        return "Read", {"file_path": e["path"]}, "", {"file": {"numLines": e["lines"]}}
    if e["kind"] == "search":
        return e["tool"], {"pattern": e["query"], "path": e["path"]}, "[result hidden]", {}
    if e["kind"] == "edit":
        hunks = e.get("hunks") or [{"oldStart": 1, "newStart": 1, "lines": [HIDDEN_CODE]}]
        return "Edit", {"file_path": e["path"]}, "", {"structuredPatch": hunks}
    return e["name"], {}, "[result hidden]", {}


def write_codex(session: Session, events: list[dict], cwd: str) -> list[dict]:
    records: list[dict] = [
        {"type": "session_meta", "timestamp": session.started, "payload": {"cwd": cwd, "cli_version": session.version}},
        {"type": "turn_context", "timestamp": session.started, "payload": {"model": session.model}},
    ]
    for e in events:
        records.append({"type": "event_msg", "timestamp": e["at"], "payload": {"type": "item_completed", "item": _codex_item(e)}})
    return records


def _codex_item(e: dict) -> dict:
    text = [{"type": "text", "text": e.get("text", "")}]
    if e["kind"] == "user":
        return {"type": "UserMessage", "content": text}
    if e["kind"] == "say":
        return {"type": "AgentMessage", "content": text}
    if e["kind"] == "shell":
        return {"type": "CommandExecution", "command": e["command"], "aggregated_output": e["output"], "exit_code": 0 if e["ok"] else 1}
    if e["kind"] == "edit":
        change = {"type": "add", "content": e["unified"] or "[code hidden]"} if e["mode"] == "create" else {
            "type": "update", "unified_diff": e["unified"] or "@@ -1,0 +1,1 @@\n" + HIDDEN_CODE}
        return {"type": "FileChange", "changes": {e["path"]: change}}
    return {"type": "McpToolCall", "server": "tool", "tool": e.get("name", "tool"),
            "result": {"content": [{"type": "text", "text": "[result hidden]"}], "isError": not e["ok"]}}


def sanitize_session(session: Session, config: Config) -> Sanitized:
    scrub = Scrubber(config.names, config.private_strings() | session.private)
    events = [clean for e in session.events if (clean := clean_event(e, scrub, config))]
    title = one_line(session.title, scrub, 80)
    # The folder name alone: where a repo sits on this machine is nobody's business.
    project = one_line(os.path.basename(session.cwd.rstrip("/")), scrub, 40)
    cwd = f"~/{project}" if project else "~"
    records = write_codex(session, events, cwd) if session.agent == "codex" else write_claude(session, events, title, cwd)
    jsonl = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    return Sanitized(session.agent, title, session.started, jsonl, scrub)


def sanitize_file(path, config: Config) -> Sanitized:
    return sanitize_session(parse_session(path), config)
