# Filmare: agent guide

Filmare turns Claude Code and Codex logs into short videos. It uses no AI and no API credits.

- **The repo is public.** Fixtures use invented data only. Real names live in `~/.config/filmare/config.toml`, outside the repo. Never commit a session log, a clip or a config.
- **Nothing passes unless a line in `filmare/sanitize.py` copies it.** Add to that allowlist with care. Never copy raw command output.
- **Gates fail closed and report counts, never values.** A new output path goes after all three gates. A missing check refuses.
- **A replacement must never match a scrub pattern,** because the scan runs those patterns over scrubbed text.
- **Every privacy change ships with a test** in `tests/test_privacy.py` or `tests/test_render.py`.
- **The renderer is pinned** in `filmare/render.py` (`REPLAY_SHA`) and `helpers/agent-replay.bun.lock`. Never install the npm package named `agent-replay`, and never call the renderer's `pr` command.
- **`./check.sh` is the gate,** and the pre-push hook runs it (`git config core.hooksPath .githooks`). Nothing runs on GitHub.
- Review with `~/ops/bin/codex-review --high`: this code handles on-device user data.
- Merge, don't squash. Never force-push. Everything in the repo is in English.
