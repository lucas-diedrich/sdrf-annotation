#!/bin/sh
#
# Entrypoint script for Claude Code container
# Handles dynamic UID/GID mapping to match host user
#
# from https://github.com/nezhar/claude-container/blob/97c7f99c56533ae6cee949f76bfd64a30d302be3/claude-code/entrypoint.sh

set -e

# Default to user 1000:1000 if not specified
USER_UID=${USER_UID:-1000}
USER_GID=${USER_GID:-1000}

# Pinned rather than left to Claude Code's default, which moves with CLI
# releases: the model is recorded as the co-author of every contributed SDRF,
# so it must not change silently between an image rebuild and the next batch.
# [1m] keeps the long-context window the annotation runs rely on.
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-opus-5-5[1m]}"

# Register the plugins and MCP servers baked into the image.
#
# The cache is copied (no-clobber) rather than referenced in place: Claude
# re-syncs a plugin from its marketplace when the cache is missing, which would
# pull the unpatched upstream plugin.json back in. The copy is NOT gated on
# plugins/ being absent -- that guard silently skipped a config dir left by an
# older image, leaving settings.json claiming the plugin is enabled while
# installed_plugins.json stayed empty, so Claude reported "No plugins installed".
#
# Existing keys always win so a manual change is never clobbered. .claude.json
# and settings.json are merged key-wise, never spread wholesale: .claude.json
# also carries machineID/userID, which must not be cloned from the build.
CFG="${CLAUDE_CONFIG_DIR:-/home/claude/.claude}"
mkdir -p "$CFG/plugins"
if [ -d /opt/claude-seed ]; then
    for d in cache marketplaces; do
        if [ -d "/opt/claude-seed/plugins/$d" ]; then
            mkdir -p "$CFG/plugins/$d"
            cp -an "/opt/claude-seed/plugins/$d/." "$CFG/plugins/$d/" 2>/dev/null || true
        fi
    done

    node -e '
      const fs = require("fs"), dir = process.argv[1], seed = "/opt/claude-seed/";
      const read = (f) => { try { return JSON.parse(fs.readFileSync(f, "utf8")); } catch { return {}; } };
      const rehome = (o) => JSON.parse(JSON.stringify(o).split("/opt/claude-seed").join(dir));
      const merge = (file, keys, spreadTop) => {
        const s = rehome(read(seed + file)), c = read(dir + "/" + file);
        const out = spreadTop ? { ...s, ...c } : c;
        for (const k of keys) out[k] = { ...(s[k] || {}), ...(c[k] || {}) };
        fs.writeFileSync(dir + "/" + file, JSON.stringify(out, null, 2));
      };
      merge("settings.json", ["enabledPlugins", "extraKnownMarketplaces"], false);
      merge(".claude.json", ["mcpServers"], false);
      merge("plugins/installed_plugins.json", ["plugins"], true);
      merge("plugins/known_marketplaces.json", [], true);
    ' "$CFG"

    # Re-apply the build-time workarounds to whatever cache Claude will read,
    # including one a previous run re-synced from the marketplace unpatched:
    # drop the bundled .mcp.json (its ./.venv/bin/python path cannot resolve, and
    # the same server is registered at user scope) and the duplicate hooks key.
    find "$CFG/plugins/cache" -name ".mcp.json" -path "*sdrf-skills*" -delete 2>/dev/null || true
    find "$CFG/plugins/cache" -name plugin.json -path "*sdrf-skills*" 2>/dev/null \
      | while read -r f; do
            node -e 'const f=process.argv[1],fs=require("fs");
                     const j=JSON.parse(fs.readFileSync(f,"utf8"));
                     if (j.hooks) { delete j.hooks; fs.writeFileSync(f,JSON.stringify(j,null,2)); }' "$f" 2>/dev/null || true
        done
fi

# Skills read spec/ by repo-root-relative path, so it must exist at the working
# directory. Symlinked rather than copied so a submodule update in the image
# takes effect without re-seeding. Dangling if inspected from the host.
if [ -d /opt/sdrf-skills/spec ] && [ ! -e /workspace/spec ]; then
    ln -s /opt/sdrf-skills/spec /workspace/spec 2>/dev/null || true
fi

# If running as root (UID 0), stay as root
if [ "$USER_UID" -eq 0 ]; then
    exec "$@"
fi

# Create group if it doesn't exist
# First check if GID is already in use
if ! getent group "$USER_GID" >/dev/null 2>&1; then
    groupadd -g "$USER_GID" claude 2>/dev/null || true
else
    # If GID exists but not with name 'claude', use existing group
    EXISTING_GROUP=$(getent group "$USER_GID" | cut -d: -f1)
    if [ -n "$EXISTING_GROUP" ] && [ "$EXISTING_GROUP" != "claude" ]; then
        GROUP_NAME="$EXISTING_GROUP"
    else
        GROUP_NAME="claude"
    fi
fi

# Default group name if not set
GROUP_NAME=${GROUP_NAME:-claude}

# Create user if it doesn't exist
# First check if UID is already in use
if ! getent passwd "$USER_UID" >/dev/null 2>&1; then
    useradd -m -u "$USER_UID" -g "$GROUP_NAME" -d /home/claude -s /bin/sh claude 2>/dev/null || true
    USER_NAME="claude"
else
    # If UID exists, use existing user
    USER_NAME=$(getent passwd "$USER_UID" | cut -d: -f1)
fi

# Ensure config directory is accessible. The seeded plugins/ tree and the
# merged settings.json must be owned by the runtime user, so unlike the
# original directory-only chown this recurses over those two paths.
if [ -d "$CFG" ]; then
    chown "$USER_UID:$USER_GID" "$CFG" 2>/dev/null || true
    chmod 755 "$CFG" 2>/dev/null || true
    chown -R "$USER_UID:$USER_GID" "$CFG/plugins" "$CFG/settings.json" "$CFG/.claude.json" 2>/dev/null || true
fi

# Don't recursively chown workspace - files created by the container will automatically
# have the correct ownership since we're running as USER_UID:USER_GID.
#
# chmod alone was not enough: /workspace is created root-owned in the image, so
# the runtime user could not create scratchpad/ under it and relocated its work
# to $HOME, outside every mount. Chown the container-layer directories
# themselves -- never -R, which would rewrite ownership of the host files
# visible through the bind mounts.
if [ -d /workspace ]; then
    chmod 755 /workspace 2>/dev/null || true
    mkdir -p /workspace/scratchpad
    chown "$USER_UID:$USER_GID" /workspace /workspace/scratchpad 2>/dev/null || true
fi

# gosu does not reset HOME, and the node base image hardcodes HOME=/home/node,
# owned by uid 1000. Any other runtime UID then has an unwritable $HOME, and
# every ~/.cache write (pooch, pip, gh) fails with a misleading error rather
# than a permission one. Point HOME at the runtime user's own home.
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
if [ -n "$USER_HOME" ]; then
    mkdir -p "$USER_HOME" 2>/dev/null || true
    chown "$USER_UID:$USER_GID" "$USER_HOME" 2>/dev/null || true
    export HOME="$USER_HOME"
fi

# Switch to the user and execute the command
# Use the actual username to ensure proper environment setup
# Set SHELL environment variable for Claude Code
export SHELL=/bin/bash
exec gosu "${USER_NAME}" "$@"
