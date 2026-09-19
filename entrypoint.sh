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

# Ensure config directory is accessible without modifying existing credential files
# Only fix ownership of the directory itself, not its contents
if [ -d /.claude ]; then
    # Change ownership of the directory itself
    chown "$USER_UID:$USER_GID" /.claude 2>/dev/null || true
    # Ensure it's writable
    chmod 755 /.claude 2>/dev/null || true
fi

# Don't recursively chown workspace - files created by the container will automatically
# have the correct ownership since we're running as USER_UID:USER_GID
# Only ensure the directory itself is accessible
if [ -d /workspace ]; then
    chmod 755 /workspace 2>/dev/null || true
fi

# Switch to the user and execute the command
# Use the actual username to ensure proper environment setup
# Set SHELL environment variable for Claude Code
export SHELL=/bin/bash
exec gosu "${USER_NAME}" "$@"
