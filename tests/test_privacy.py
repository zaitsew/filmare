"""Every name, address and key below is invented."""
import json
import tempfile
import unittest
from pathlib import Path

from filmare.config import Config, add_private, harvest
from filmare.logs import parse_session
from filmare.sanitize import clip_text, sanitize_file, sanitize_session, status_of
from filmare.scrub import Scrubber

FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
SECRET_OUTPUT = f"export GH_TOKEN={FAKE_TOKEN}\nzelda@example.org lives at Calle Falsa 123\nplain noise line"
CONFIG = Config(names=[r"Zelda\w*", r"Зельд\w*"], show_code=True)


def write_lines(records) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for record in records:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    handle.close()
    return Path(handle.name)


def claude_log() -> Path:
    def assistant(*blocks, **extra):
        return {"type": "assistant", "timestamp": "2026-11-01T10:00:00Z", "cwd": "/Users/someone/repos/demo",
                "version": "2.1", "message": {"model": "claude-test", "content": list(blocks)}, **extra}

    def user(*blocks, **extra):
        return {"type": "user", "timestamp": "2026-11-01T10:00:01Z", "message": {"content": list(blocks)}, **extra}

    return write_lines([
        {"type": "custom-title", "customTitle": "Fix for Zelda"},
        {"type": "user", "timestamp": "2026-11-01T10:00:00Z", "cwd": "/Users/someone/repos/demo",
         "message": {"content": "Please fix the trip for Зельде, mail zelda@example.org"}},
        assistant({"type": "thinking", "thinking": "THINKING-MUST-NOT-APPEAR"}),
        assistant({"type": "tool_use", "id": "b1", "name": "Bash",
                   "input": {"command": f"curl -H 'Authorization: Bearer abcdef123456' --token {FAKE_TOKEN} x\nsecond line"}}),
        user({"type": "tool_result", "tool_use_id": "b1", "content": SECRET_OUTPUT},
             toolUseResult={"stdout": SECRET_OUTPUT + "\nTests 3 passed (3)"}),
        assistant({"type": "tool_use", "id": "b2", "name": "Bash", "input": {"command": "cat notes"}}),
        user({"type": "tool_result", "tool_use_id": "b2", "content": "just\nsome\nnotes"},
             toolUseResult={"stdout": "just\nsome\nnotes"}),
        assistant({"type": "tool_use", "id": "m1", "name": "mcp__trips__get", "input": {"name": "Private Villa Sol"}}),
        user({"type": "tool_result", "tool_use_id": "m1",
              "content": json.dumps({"address": "Avenida Inventada 7, Testville", "guest": "Quentin Blueberry"})}),
        assistant({"type": "text", "text": "Quentin Blueberry stays at Avenida Inventada 7 in Private Villa Sol."}),
        assistant({"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": "/Users/someone/repos/demo/App.swift"}}),
        user({"type": "tool_result", "tool_use_id": "e1", "content": ""},
             toolUseResult={"structuredPatch": [{"oldStart": 1, "newStart": 1, "lines": ["-old", f"+let key = \"{FAKE_TOKEN}\""]}]}),
        assistant({"type": "tool_use", "id": "e2", "name": "Write",
                   "input": {"file_path": "/Users/someone/repos/demo/data/trip.json", "content": "PRIVATE-FILE-BODY"}}),
        user({"type": "tool_result", "tool_use_id": "e2", "content": ""}),
        user({"type": "text", "text": "<task-notification>NOTICE-MUST-NOT-APPEAR</task-notification>"}),
        user({"type": "image", "source": {"data": "IMAGE-BYTES-MUST-NOT-APPEAR"}}),
        assistant({"type": "text", "text": "SIDECHAIN-MUST-NOT-APPEAR"}, isSidechain=True),
    ])


def codex_log() -> Path:
    def item(payload):
        return {"type": "event_msg", "timestamp": "2026-11-01T10:00:00Z", "payload": {"type": "item_completed", "item": payload}}

    return write_lines([
        {"type": "session_meta", "timestamp": "2026-11-01T10:00:00Z", "payload": {"cwd": "/Users/someone/repos/demo", "cli_version": "0.1"}},
        {"type": "turn_context", "payload": {"model": "gpt-test"}},
        item({"type": "UserMessage", "content": [{"type": "text", "text": "<environment_context>INJECTED</environment_context>"}]}),
        item({"type": "UserMessage", "content": [{"type": "text", "text": "Fix it for Zelda"}]}),
        item({"type": "Reasoning", "summary_text": ["REASONING-MUST-NOT-APPEAR"]}),
        item({"type": "CommandExecution", "command": ["/bin/zsh", "-lc", f"deploy --token {FAKE_TOKEN}"],
              "aggregated_output": SECRET_OUTPUT, "exit_code": 1}),
        item({"type": "FileChange", "changes": {"/Users/someone/repos/demo/a.py": {"type": "update", "unified_diff": f"@@ -1 +1 @@\n+k = '{FAKE_TOKEN}'"}}}),
        item({"type": "McpToolCall", "server": "trips", "tool": "get", "arguments": {"name": "Private Villa Sol"},
              "result": {"content": [{"type": "text", "text": json.dumps({"guest": "Quentin Blueberry"})}]}}),
        item({"type": "AgentMessage", "content": [{"type": "text", "text": "Quentin Blueberry is in Private Villa Sol."}]}),
    ])


class ScrubberTests(unittest.TestCase):
    CASES = [
        (f"token is {FAKE_TOKEN}", FAKE_TOKEN),
        ("Authorization: Bearer abcdef123456", "abcdef123456"),
        ("GH_TOKEN=abcdef123456", "abcdef123456"),
        ("--password hunter22", "hunter22"),
        ("deploy --password 'hunter 22'", "hunter 22"),
        ('API_KEY="hunter 22"', "hunter 22"),
        ("Authorization: Basic YWxpY2U6aHVudGVyMg==", "YWxpY2U6aHVudGVyMg"),
        ("curl -u alice:hunter22 x", "hunter22"),
        ("https://user:hunter22@example.org/x", "hunter22"),
        ("a@b.example.org", "a@b.example.org"),
        ("+34 612 345 678", "612 345 678"),
        ("612 345 678", "612 345 678"),
        ("id 123e4567-e89b-12d3-a456-426614174000", "123e4567"),
        ("host 203.0.113.9", "203.0.113.9"),
        ("/Users/someone/repos", "someone"),
        ("/tmp/secret-file-name", "secret-file-name"),
        ("-----BEGIN RSA " + "PRIVATE KEY-----\nabc\n-----END RSA " + "PRIVATE KEY-----", "abc"),
    ]

    def test_removes_each_kind(self):
        scrub = Scrubber()
        for text, secret in self.CASES:
            self.assertNotIn(secret, scrub(text), text)

    def test_output_scans_clean_and_is_stable(self):
        scrub = Scrubber(["Zelda"], {"Calle Falsa 123"})
        for text, _ in self.CASES + [("Zelda at Calle Falsa 123", "")]:
            once = scrub(text)
            self.assertEqual(scrub(once), once, text)
            self.assertFalse(scrub.scan(once), f"{text!r} -> {once!r}")

    def test_leaves_ordinary_text_alone(self):
        scrub = Scrubber()
        for text in ("Tests 37 passed", "merged #679 on 2026-11-01", "git log --oneline HEAD..main", "tokens used: 12"[:12]):
            self.assertEqual(scrub(text), text)

    def test_private_string_matches_whole_words_only(self):
        scrub = Scrubber(private={"Mall"})
        self.assertEqual(scrub("Small mall"), "Small [private]")

    def test_private_phrase_survives_line_wrapping(self):
        scrub = Scrubber(private={"Calle Falsa 123"})
        self.assertEqual(scrub("at Calle\n  Falsa 123"), "at [private]")
        self.assertEqual(scrub.scan("at Calle\n  Falsa 123")["private"], 1)
        self.assertGreaterEqual(scrub.scan("at Calle Fal\nsa 123")["private"], 1)

    def test_scan_reports_categories_not_values(self):
        found = Scrubber(["Zelda"]).scan(f"Zelda {FAKE_TOKEN}")
        self.assertEqual(set(found), {"name", "token"})
        self.assertNotIn(FAKE_TOKEN, repr(found))


class SanitizeTests(unittest.TestCase):
    MUST_NOT_APPEAR = (
        "THINKING-MUST", "NOTICE-MUST", "IMAGE-BYTES", "SIDECHAIN-MUST", "REASONING-MUST", "PRIVATE-FILE-BODY",
        "INJECTED", "plain noise", "abcdef123456", "someone", "second line", "example.org", "Calle Falsa",
        "Zelda", "Зельд", "Quentin", "Avenida Inventada", "Private Villa", FAKE_TOKEN,
    )

    def check(self, result):
        for needle in self.MUST_NOT_APPEAR:
            self.assertNotIn(needle, result.jsonl, needle)
        self.assertFalse(result.scan(), result.scan())

    def test_claude_log(self):
        result = sanitize_file(claude_log(), CONFIG)
        self.check(result)
        self.assertEqual(result.agent, "claude")
        self.assertIn('"cwd": "~/demo"', result.jsonl)
        self.assertIn("3 passed", result.jsonl)
        self.assertIn("[3 lines hidden]", result.jsonl)
        self.assertIn("[private]", result.jsonl)
        self.assertIn("[Image]", result.jsonl)

    def test_codex_log_stays_a_codex_log(self):
        result = sanitize_file(codex_log(), CONFIG)
        self.check(result)
        self.assertEqual(result.agent, "codex")
        self.assertEqual(json.loads(result.jsonl.splitlines()[0])["type"], "session_meta")
        self.assertIn('"exit_code": 1', result.jsonl)

    def test_code_is_hidden_unless_config_allows_it(self):
        for log in (claude_log(), codex_log()):
            hidden = sanitize_file(log, Config(names=[r"Zelda\w*", r"Зельд\w*"], show_code=False))
            self.assertIn("[code hidden]", hidden.jsonl)
            self.assertNotIn("let key", hidden.jsonl)
            self.check(hidden)

    def test_private_files_stay_hidden_even_with_show_code(self):
        self.assertNotIn("PRIVATE-FILE-BODY", sanitize_file(claude_log(), CONFIG).jsonl)

    def test_a_leak_is_caught_after_sanitizing(self):
        result = sanitize_file(claude_log(), CONFIG)
        result.jsonl += json.dumps({"x": "Zelda " + FAKE_TOKEN}) + "\n"
        self.assertEqual(set(result.scan()), {"name", "token"})

    def test_zstd_logs_are_read(self):
        from compression import zstd

        plain = claude_log().read_bytes()
        path = Path(tempfile.mkdtemp()) / "rollout.jsonl.zst"
        path.write_bytes(zstd.compress(plain))
        self.assertTrue(parse_session(path).events)

    def test_session_with_no_private_data_still_works(self):
        session = parse_session(claude_log())
        session.events = session.events[:1]
        self.assertIn("[name]", sanitize_session(session, CONFIG).jsonl)


class ReviewFixTests(unittest.TestCase):
    def test_generic_rules_do_not_break_a_private_phrase(self):
        scrub = Scrubber(private={"Quentin Blueberry quentin@example.org"})
        self.assertEqual(scrub("Quentin Blueberry quentin@example.org"), "[private]")

    def test_a_masked_name_does_not_leave_its_email_domain(self):
        scrub = Scrubber(["Zelda"], {"Quentin Blueberry"})
        self.assertEqual(scrub("zelda@example.org"), "[email]")
        self.assertEqual(scrub("quentin.blueberry@example.org"), "[email]")

    def test_numeric_values_under_sensitive_keys_are_private(self):
        found = set()
        harvest({"phone": "612345678", "pnr": 123456, "from": "2026-11-01"}, found)
        self.assertEqual(found, {"612345678", "123456"})
        scrub = Scrubber(private=found)
        self.assertEqual(scrub("Call 612345678"), "Call [private]")
        self.assertEqual(scrub("Call 612-345-678"), "Call [private]")

    def test_status_never_copies_a_line(self):
        output = "Quentin Blueberry passed verification\nTests 3 passed (3)\nBUILD SUCCEEDED for Quentin"
        self.assertEqual(status_of(output, True), "3 passed · BUILD SUCCEEDED")
        self.assertEqual(status_of("Quentin passed\nfine", True), "[2 lines hidden]")
        self.assertEqual(status_of("", False), "[failed]")

    def test_scan_reads_dictionary_keys(self):
        result = sanitize_file(codex_log(), CONFIG)
        result.jsonl += json.dumps({"changes": {"QuentinBlueberry.py": {}}}) + "\n"
        self.assertTrue(result.scan()["private"])

    def test_separated_forms_of_a_phrase_are_masked(self):
        scrub = Scrubber(private={"Quentin Blueberry"})
        for form in ("QuentinBlueberry.py", "quentin_blueberry", "quentin-blueberry"):
            self.assertNotIn("uentin", scrub(form), form)

    def test_each_mcp_block_is_harvested(self):
        def assistant(*blocks):
            return {"type": "assistant", "timestamp": "t", "message": {"content": list(blocks)}}

        log = write_lines([
            assistant({"type": "tool_use", "id": "m", "name": "mcp__trips__get", "input": {}}),
            {"type": "user", "timestamp": "t", "message": {"content": [{"type": "tool_result", "tool_use_id": "m", "content": [
                {"type": "text", "text": json.dumps({"guest": "Quentin Blueberry"})},
                {"type": "text", "text": json.dumps({"address": "Avenida Inventada 7"})},
            ]}]}},
            assistant({"type": "text", "text": "Quentin Blueberry is at Avenida Inventada 7."}),
        ])
        self.assertEqual(parse_session(log).private, {"Quentin Blueberry", "Avenida Inventada 7"})
        self.assertNotIn("Quentin", sanitize_file(log, Config()).jsonl)


class ClipTests(unittest.TestCase):
    def test_cuts_at_a_sentence(self):
        self.assertEqual(clip_text("One sentence here. Two sentences there and more words", 30), "One sentence here. …")

    def test_short_text_is_untouched(self):
        self.assertEqual(clip_text("Short.", 30), "Short.")

    def test_no_sentence_cuts_at_a_word(self):
        self.assertEqual(clip_text("alpha beta gamma delta", 12), "alpha beta …")


class ConfigTests(unittest.TestCase):
    def test_harvest_takes_listed_keys_only(self):
        found = set()
        harvest({"guest": "Quentin", "id": "abcdef", "list": [{"address": "Calle Mayor 12, Testville"}]}, found)
        self.assertEqual(found, {"Quentin", "Calle Mayor 12, Testville", "Calle Mayor 12"})

    def test_dates_numbers_and_short_values_are_not_names(self):
        found = set()
        for value in ("2026-11-01", "12:30", "abc", "true", "x" * 121):
            add_private(value, found)
        self.assertEqual(found, set())

    def test_allow_list_wins(self):
        found = set()
        add_private("Valencia", found, frozenset({"valencia"}))
        self.assertEqual(found, set())

    def test_missing_config_is_an_error(self):
        from filmare.config import ConfigError

        with self.assertRaises(ConfigError):
            Config.load(Path(tempfile.mkdtemp()) / "none.toml")


if __name__ == "__main__":
    unittest.main()
