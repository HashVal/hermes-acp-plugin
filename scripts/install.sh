#!/usr/bin/env bash
# Install agent-bridge into a Hermes home.
#   scripts/install.sh            copy the plugin (default)
#   scripts/install.sh --link     symlink the plugin to this repo (edit here, restart gateway)
# Does not restart the gateway and does not overwrite an existing config.json.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DST="$HERMES_HOME/plugins/agent-bridge"
ADAPTERS_DST="$HERMES_HOME/acp-adapters"
BRIDGE_HOME="$HERMES_HOME/agent-bridge"

mode="copy"
[[ "${1:-}" == "--link" ]] && mode="link"

# 1. Plugin code
mkdir -p "$HERMES_HOME/plugins"
if [[ -e "$PLUGIN_DST" || -L "$PLUGIN_DST" ]]; then
  backup="$PLUGIN_DST.bak-$(date +%Y%m%d-%H%M%S)"
  echo "Moving existing $PLUGIN_DST -> $backup"
  mv "$PLUGIN_DST" "$backup"
fi
if [[ "$mode" == "link" ]]; then
  ln -s "$REPO/plugin/agent-bridge" "$PLUGIN_DST"
else
  mkdir -p "$PLUGIN_DST"
  cp "$REPO"/plugin/agent-bridge/*.py "$REPO"/plugin/agent-bridge/plugin.yaml "$PLUGIN_DST/"
fi
echo "Plugin installed ($mode): $PLUGIN_DST"

# 2. ACP adapters (exact versions from package-lock.json)
mkdir -p "$ADAPTERS_DST"
cp "$REPO/adapters/package.json" "$REPO/adapters/package-lock.json" "$ADAPTERS_DST/"
(cd "$ADAPTERS_DST" && npm ci)
echo "Adapters installed: $ADAPTERS_DST/node_modules/.bin"

# 3. Bridge config (never overwritten)
mkdir -p "$BRIDGE_HOME"
if [[ ! -f "$BRIDGE_HOME/config.json" ]]; then
  cp "$REPO/config/config.example.json" "$BRIDGE_HOME/config.json"
  echo "Created $BRIDGE_HOME/config.json from the example. Set allowed_users before use."
else
  echo "Kept existing $BRIDGE_HOME/config.json"
fi

cat <<MSG

Next:
  1. Add 'agent-bridge' to plugins.enabled in $HERMES_HOME/config.yaml
  2. systemctl --user restart hermes-gateway
MSG
