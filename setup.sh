#!/usr/bin/env bash
# Installs what Filmare needs into ~/.local/share/filmare. Safe to run again.
set -euo pipefail

REPLAY_REPO="https://github.com/vkpdeveloper/agent-replay"
REPLAY_SHA="$(sed -n 's/^REPLAY_SHA = "\(.*\)"$/\1/p' "$(dirname "$0")/filmare/render.py")"
root="$(cd "$(dirname "$0")" && pwd)"
data="${FILMARE_DATA:-$HOME/.local/share/filmare}"

for tool in bun ffmpeg ffprobe git python3 xcrun; do
  command -v "$tool" >/dev/null || { echo "Missing: $tool. Install it, then run setup.sh again." >&2; exit 2; }
done

mkdir -p "$data/bin" "$data/home" "$data/work"
chmod 700 "$data"

# The renderer is pinned. The npm package named agent-replay is a different project.
if [ ! -d "$data/agent-replay/.git" ]; then
  git clone --quiet "$REPLAY_REPO" "$data/agent-replay"
fi
git -C "$data/agent-replay" fetch --quiet origin "$REPLAY_SHA"
# Force and clean: a pinned HEAD proves nothing about edited or added files.
git -C "$data/agent-replay" checkout --quiet --force --detach "$REPLAY_SHA"
git -C "$data/agent-replay" clean -fdq
[ "$(git -C "$data/agent-replay" rev-parse HEAD)" = "$REPLAY_SHA" ] || { echo "Renderer is not at the pinned commit." >&2; exit 2; }
# Our own lockfile: the renderer's cannot be parsed by this bun, which would then float every dependency.
cp "$root/helpers/agent-replay.bun.lock" "$data/agent-replay/bun.lock"
(cd "$data/agent-replay" && bun install --frozen-lockfile --ignore-scripts >/dev/null && bun scripts/build.ts --player >/dev/null)

# Xcode's SDK when it exists; on this kind of Mac a stale SDKROOT breaks swiftc.
if [ -d /Applications/Xcode.app ]; then
  export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
fi
unset SDKROOT
xcrun --sdk macosx swiftc -O "$root/helpers/ocr.swift" -o "$data/bin/ocr"
xcrun --sdk macosx swiftc -O "$root/helpers/label.swift" -o "$data/bin/label"

config="$HOME/.config/filmare/config.toml"
if [ ! -f "$config" ]; then
  mkdir -p "$(dirname "$config")"
  install -m 600 "$root/config.example.toml" "$config"
  echo "Created $config. Add the names and files to keep out of your videos."
fi

mkdir -p "$HOME/.local/bin"
ln -sf "$root/bin/filmare" "$HOME/.local/bin/filmare"
"$root/bin/filmare" doctor
