"""Removes secrets and personal data from text, and reports what still looks like them."""
from __future__ import annotations

import re
from collections import Counter

# (category, pattern, replacement). The order matters: credentials go before the broad patterns.
# A replacement must never match any pattern, because the leak scan runs these over scrubbed text.
_SENSITIVE = r"[\w-]*(?:token|secret|passw(?:or)?d|passphrase|api[_-]?key|apikey|access[_-]?key|private[_-]?key)[\w-]*"
PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"), "[key]"),
    ("credential", re.compile(r"""(?i)(\b(?:proxy-)?authorization\b["']?\s*[:=]\s*["']?)(?!\[)(?:\w+\s+)?(?!\[)[^\s"',;]+"""), r"\1[hidden]"),
    ("token", re.compile(r"(?i)\b(bearer|basic)\s+(?!\[)[\w.~+/=-]{6,}"), r"\1 [token]"),
    ("token", re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), "[token]"),
    ("token", re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|glpat|xai|sbp|sb_secret|sb_publishable|github_pat|npm|hf|gsk)[-_][\w-]{12,}"), "[token]"),
    ("token", re.compile(r"\bAKIA[0-9A-Z]{16}\b|\bAIza[\w-]{35}"), "[token]"),
    ("credential", re.compile(rf"""(?i)(\b{_SENSITIVE}["']?\s*[:=]\s*)(?!\[)(?:"[^"\n]*"|'[^'\n]*'|[^\s"',;]+)"""), r"\1[hidden]"),
    ("credential", re.compile(r"""(?i)((?<![\w-])--?(?:password|passwd|pass|token|api-key|apikey|secret|auth)[= ]+)(?!\[)(?:"[^"\n]*"|'[^'\n]*'|[^\s"']+)"""), r"\1[hidden]"),
    ("credential", re.compile(r"""((?<![\w-])(?:-u|--user)[= ]+)(?!\[)["']?[^\s"':]+:[^\s"']+["']?"""), r"\1[hidden]"),
    ("credential", re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1"),
    ("email", re.compile(r"[\w.+\[\]-]+@[\w-]+\.[\w.-]*\w"), "[email]"),
    ("phone", re.compile(r"(?<![\w.])\+\d[\d ()-]{7,}\d(?![\w.])"), "[number]"),
    ("phone", re.compile(r"(?<![\w.-])\d{3}[ -]\d{2,3}[ -]\d{2,3}(?:[ -]\d{2,3})?(?![\w.-])"), "[number]"),
    ("id", re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "[id]"),
    ("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[ip]"),
    ("token", re.compile(r"\b(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[\w-]{32,}\b"), "[token]"),
    ("project-ref", re.compile(r"\b[a-z]{20}\b"), "[ref]"),
    ("path", re.compile(r"(?:/private)?/(?:tmp|var/folders)/[^\s\"')]+"), "[tmp]"),
    ("path", re.compile(r"/Users/[^/\s\"')]+"), "~"),
]
SYSTEM_TEXT = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>")
PASTED_TEXT = re.compile(r"<pasted_content\b[^>]*>[\s\S]*?</pasted_content\b[^>]*>")


def _phrase(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 7 and not re.search(r"[^\W\d_]", value):
        # A phone number or booking code, however it is punctuated.
        return r"\+?" + r"[\s().-]*".join(digits)
    # Separators may change too: "Jane Doe" also appears as JaneDoe.py or jane_doe.
    return r"[\s_.-]*".join(re.escape(part) for part in value.split())


class Scrubber:
    def __init__(self, names: list[str] | None = None, private: set[str] | None = None):
        self.names = re.compile("|".join(f"(?:{n})" for n in names), re.I) if names else None
        values = sorted(private or (), key=len, reverse=True)
        self.private = (
            re.compile(r"(?<!\w)(?:" + "|".join(_phrase(v) for v in values) + r")(?!\w)", re.I) if values else None
        )
        # Wrapping can break a phrase inside a word, so the scan also compares with whitespace removed.
        self._squeezed = [s for s in (re.sub(r"\s+", "", v).lower() for v in values) if len(s) >= 8]

    def __call__(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        text = SYSTEM_TEXT.sub("", text)
        text = PASTED_TEXT.sub("[pasted text hidden]", text)
        # Private phrases first: a generic rule could eat part of one and hide the rest from the match.
        if self.private:
            text = self.private.sub("[private]", text)
        for _, pattern, replacement in PATTERNS:
            text = pattern.sub(replacement, text)
        if self.names:
            text = self.names.sub("[name]", text)
        return text

    def scan(self, text: str) -> Counter:
        """Counts what still looks private. Returns categories only, so a report cannot leak values."""
        found: Counter = Counter()
        flat = " ".join(text.split())
        for category, pattern, _ in PATTERNS:
            hits = len(pattern.findall(flat))
            if hits:
                found[category] += hits
        if self.names and (hits := len(self.names.findall(flat))):
            found["name"] += hits
        if self.private and (hits := len(self.private.findall(flat))):
            found["private"] += hits
        squeezed = re.sub(r"\s+", "", text).lower()
        if hits := sum(1 for value in self._squeezed if value in squeezed):
            found["private"] = max(found["private"], hits)
        return found
