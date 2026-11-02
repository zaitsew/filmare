"""User settings. They live outside the repo because they name private things."""
from __future__ import annotations

import glob
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("~/.config/filmare/config.toml").expanduser()

# Values under these keys in structured data are treated as private.
DEFAULT_KEYS = frozenset({
    "address", "airline", "booking", "city", "confirmation", "email", "flight", "from", "guest", "host",
    "hotel", "label", "name", "note", "notes", "phone", "place", "pnr", "reference", "ref", "street",
    "title", "to", "venue",
})
ALWAYS_ALLOWED = frozenset({"true", "false", "none", "null"})
NOT_A_NAME = re.compile(r"^[\d\s:./+-]+$")
# Under these keys a bare number is the secret itself.
NUMERIC_KEYS = frozenset({"booking", "confirmation", "phone", "pnr", "ref", "reference"})


class ConfigError(Exception):
    pass


@dataclass
class Config:
    names: list[str] = field(default_factory=list)  # regexes, so stems catch case endings
    deny_text: list[str] = field(default_factory=list)  # files with one private string per line
    deny_json: list[str] = field(default_factory=list)  # globs of JSON files to harvest
    deny_keys: frozenset[str] = DEFAULT_KEYS
    allow: frozenset[str] = ALWAYS_ALLOWED
    show_code: bool = False
    say_limit: int = 280

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "Config":
        try:
            raw = tomllib.loads(Path(path).read_text())
        except FileNotFoundError:
            raise ConfigError(f"no config at {path}; run ./setup.sh or copy config.example.toml") from None
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"{path} is not valid TOML: {error}") from None
        names = list(raw.get("names", []))
        for name in names:
            try:
                re.compile(name)
            except re.error as error:
                raise ConfigError(f"names has a bad pattern: {error}") from None
        return cls(
            names=names,
            deny_text=list(raw.get("deny_text", [])),
            deny_json=list(raw.get("deny_json", [])),
            deny_keys=DEFAULT_KEYS | frozenset(k.lower() for k in raw.get("deny_keys", [])),
            allow=ALWAYS_ALLOWED | frozenset(w.lower() for w in raw.get("allow", [])),
            show_code=bool(raw.get("show_code", False)),
            say_limit=int(raw.get("say_limit", 280)),
        )

    def is_empty(self) -> bool:
        return not (self.names or self.deny_text or self.deny_json)

    def private_strings(self) -> set[str]:
        found: set[str] = set()
        for pattern in self.deny_text:
            for path in glob.glob(os.path.expanduser(pattern)):
                for line in Path(path).read_text().splitlines():
                    add_private(line, found, self.allow)
        for pattern in self.deny_json:
            for path in glob.glob(os.path.expanduser(pattern)):
                try:
                    harvest(json.loads(Path(path).read_text()), found, self.deny_keys, self.allow)
                except (OSError, ValueError):
                    continue
        return found


def add_private(value: str, found: set[str], allow: frozenset[str] = ALWAYS_ALLOWED, numeric: bool = False) -> None:
    value = value.strip()
    if not 4 <= len(value) <= 120 or value.lower() in allow or (NOT_A_NAME.match(value) and not numeric):
        return
    found.add(value)
    # "Calle Mayor 12, Valencia" also leaks as "Calle Mayor 12".
    head = value.split(",")[0].strip()
    if head != value and len(head) >= 6 and head.lower() not in allow and not NOT_A_NAME.match(head):
        found.add(head)


def harvest(node, found: set[str], keys: frozenset[str] = DEFAULT_KEYS,
            allow: frozenset[str] = ALWAYS_ALLOWED, key: str = "") -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            harvest(v, found, keys, allow, str(k))
    elif isinstance(node, list):
        for v in node:
            harvest(v, found, keys, allow, key)
    elif isinstance(node, str) and key.lower() in keys:
        add_private(node, found, allow, key.lower() in NUMERIC_KEYS)
    elif isinstance(node, int) and not isinstance(node, bool) and key.lower() in NUMERIC_KEYS:
        add_private(str(node), found, allow, True)
