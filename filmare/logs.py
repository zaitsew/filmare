"""Reads Claude Code and Codex session logs into one list of events.

These events still hold raw, private text. Only sanitize.py may turn them into output.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_KEYS, harvest

# Records that arrive as user messages but that nobody typed.
INJECTED = re.compile(
    r"^\s*(?:<(?:task-notification|side-sessions-event|ci-monitor-event|system-reminder|local-command|"
    r"environment_context|user_instructions|INSTRUCTIONS|turn_aborted|skill)\b|# AGENTS\.md instructions|Caveat: )"
)


@dataclass
class Session:
    agent: str
    path: str
    title: str = ""
    cwd: str = ""
    model: str = ""
    version: str = ""
    started: str = ""
    events: list[dict] = field(default_factory=list)
    private: set[str] = field(default_factory=set)  # strings this session read from private tools


def read_lines(path: str | Path):
    path = Path(path)
    if path.suffix == ".zst":
        from compression import zstd  # Python 3.14+

        handle = zstd.open(path, "rt", encoding="utf-8", errors="replace")
    else:
        handle = open(path, encoding="utf-8", errors="replace")
    with handle as lines:
        for line in lines:
            try:
                yield json.loads(line)
            except ValueError:
                continue  # a session still being written ends in a partial line


def _harvest_text(text: str, session: Session) -> None:
    try:
        harvest(json.loads(text), session.private, DEFAULT_KEYS)
    except (TypeError, ValueError):
        pass


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_claude(path) -> Session:
    session = Session("claude", str(path))
    calls: dict[str, dict] = {}
    for rec in read_lines(path):
        if rec.get("type") == "custom-title":
            session.title = rec.get("customTitle") or session.title
        if rec.get("aiTitle") and not session.title:
            session.title = rec["aiTitle"]
        if rec.get("type") not in ("user", "assistant") or rec.get("isSidechain") or rec.get("isMeta"):
            continue
        at = rec.get("timestamp", "")
        session.started = session.started or at
        session.cwd = session.cwd or rec.get("cwd", "")
        session.version = session.version or rec.get("version", "")
        message = rec.get("message") or {}
        if message.get("model") and message["model"] != "<synthetic>":
            session.model = message["model"]
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        result = rec.get("toolUseResult") if isinstance(rec.get("toolUseResult"), dict) else {}
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = block.get("text", "")
                if rec["type"] == "assistant":
                    session.events.append({"kind": "say", "at": at, "text": text})
                elif not INJECTED.match(text) and text.strip():
                    session.events.append({"kind": "user", "at": at, "text": text})
            elif kind == "image" and rec["type"] == "user":
                session.events.append({"kind": "user", "at": at, "text": "[Image]"})
            elif kind == "tool_use":
                event = _claude_tool(block.get("name", ""), block.get("input") or {}, at)
                calls[block.get("id")] = event
                session.events.append(event)
            elif kind == "tool_result" and block.get("tool_use_id") in calls:
                _claude_result(calls[block["tool_use_id"]], block, result, session)
    return session


def _claude_tool(name: str, raw: dict, at: str) -> dict:
    if name == "Bash":
        return {"kind": "shell", "at": at, "command": raw.get("command", ""), "output": "", "ok": True}
    if name == "Read":
        return {"kind": "read", "at": at, "path": raw.get("file_path", ""), "lines": 0, "ok": True}
    if name in ("Grep", "Glob"):
        return {"kind": "search", "tool": name, "at": at, "query": raw.get("pattern", ""),
                "path": raw.get("path", ""), "ok": True}
    if name in ("Edit", "MultiEdit", "Write"):
        lines = []
        if name == "Write":
            lines = ["+" + line for line in str(raw.get("content", "")).split("\n")]
        else:
            for edit in raw.get("edits", [raw]) if isinstance(raw.get("edits", [raw]), list) else [raw]:
                lines += ["-" + l for l in str(edit.get("old_string", "")).split("\n")]
                lines += ["+" + l for l in str(edit.get("new_string", "")).split("\n")]
        return {"kind": "edit", "at": at, "path": raw.get("file_path", ""),
                "mode": "create" if name == "Write" else "update",
                "hunks": [{"oldStart": 1, "newStart": 1, "lines": lines}], "ok": True}
    return {"kind": "tool", "at": at, "name": name, "input": raw, "ok": True}


def _claude_result(event: dict, block: dict, result: dict, session: Session) -> None:
    event["ok"] = not block.get("is_error")
    text = _block_text(block.get("content"))
    if event["kind"] == "shell":
        event["output"] = "\n".join(s for s in (result.get("stdout"), result.get("stderr")) if s) or text
    elif event["kind"] == "read":
        event["lines"] = (result.get("file") or {}).get("numLines", 0)
    elif event["kind"] == "edit" and result.get("structuredPatch"):
        event["hunks"] = result["structuredPatch"]
    elif event["kind"] == "tool" and event["name"].startswith("mcp__"):
        # What a private tool returned is private, and the agent may quote it later.
        _harvest_text(text, session)
        harvest(event.get("input"), session.private, DEFAULT_KEYS)


def parse_codex(path) -> Session:
    session = Session("codex", str(path))
    for rec in read_lines(path):
        payload = rec.get("payload") or {}
        at = rec.get("timestamp", "")
        session.started = session.started or at
        if rec.get("type") == "session_meta":
            session.cwd = payload.get("cwd", session.cwd)
            session.version = payload.get("cli_version", session.version)
            continue
        if rec.get("type") == "turn_context":
            session.model = payload.get("model", session.model)
            continue
        item = payload.get("item") if payload.get("type") == "item_completed" else None
        if isinstance(item, dict):
            _codex_item(item, at, session)
    return session


def _codex_item(item: dict, at: str, session: Session) -> None:
    kind = item.get("type")
    if kind in ("UserMessage", "AgentMessage"):
        text = "\n".join(p.get("text", "") for p in item.get("content") or [] if isinstance(p, dict)).strip()
        if kind == "AgentMessage" and text:
            session.events.append({"kind": "say", "at": at, "text": text})
        elif kind == "UserMessage" and text and not INJECTED.match(text):
            session.title = session.title or text.split("\n")[0][:80]
            session.events.append({"kind": "user", "at": at, "text": text})
    elif kind == "CommandExecution":
        command = item.get("command")
        if isinstance(command, list):
            # ["/bin/zsh", "-lc", "<script>"] -> "<script>"
            shell = len(command) == 3 and str(command[0]).endswith("sh") and str(command[1]).startswith("-")
            command = command[2] if shell else " ".join(map(str, command))
        session.events.append({"kind": "shell", "at": at, "command": str(command or ""),
                               "output": item.get("aggregated_output") or "",
                               "ok": item.get("exit_code") in (0, None)})
    elif kind == "FileChange":
        for path, change in (item.get("changes") or {}).items():
            change = change if isinstance(change, dict) else {}
            added = change.get("type") == "add"
            text = change.get("content") if added else change.get("unified_diff")
            session.events.append({"kind": "edit", "at": at, "path": path, "mode": "create" if added else "update",
                                   "unified": str(text or ""), "ok": True})
    elif kind == "McpToolCall":
        harvest(item.get("arguments"), session.private, DEFAULT_KEYS)
        for part in (item.get("result") or {}).get("content") or []:
            if isinstance(part, dict):
                _harvest_text(part.get("text", ""), session)
        session.events.append({"kind": "tool", "at": at, "name": f"{item.get('server')}.{item.get('tool')}",
                               "ok": not item.get("error") and not (item.get("result") or {}).get("isError")})


def parse_session(path) -> Session:
    for rec in read_lines(path):
        return parse_codex(path) if rec.get("type") == "session_meta" else parse_claude(path)
    raise ValueError(f"{path} holds no records")
