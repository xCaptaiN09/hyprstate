"""
Hyprstate Configuration Module.

Defines all constants, paths, class mappings, and environment whitelist
used by the daemon and restoration engine. No environment blacklist is used;
only variables explicitly listed in ENV_WHITELIST are preserved to prevent
stale session-key leakage across boots.
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import FrozenSet


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _resolve_hyprland_socket_dir() -> Path:
    """
    Resolve the Hyprland IPC socket directory using environment variables.

    Returns the path to: $XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/
    Raises RuntimeError if the required environment variables are missing or
    the directory does not exist.
    """
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not xdg_runtime:
        raise RuntimeError(
            "XDG_RUNTIME_DIR is not set. "
            "Hyprstate must run inside a Wayland/Hyprland user session."
        )

    hypr_sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not hypr_sig:
        raise RuntimeError(
            "HYPRLAND_INSTANCE_SIGNATURE is not set. "
            "Hyprstate requires an active Hyprland session."
        )

    socket_dir = Path(xdg_runtime) / "hypr" / hypr_sig
    if not socket_dir.is_dir():
        raise RuntimeError(
            f"Hyprland socket directory does not exist: {socket_dir}"
        )

    return socket_dir


# State storage directory — atomic writes go here
STATE_DIR: Path = Path(
    os.environ.get("HYPRSTATE_STATE_DIR", "~/.config/hyprstate")
).expanduser()

STATE_FILE: Path = STATE_DIR / "session.json"
STATE_TMP_FILE: Path = STATE_DIR / "session.json.tmp"

# Log file
LOG_FILE: Path = STATE_DIR / "hyprstate.log"


# ---------------------------------------------------------------------------
# Environment Whitelist (Strict — no blacklist)
# ---------------------------------------------------------------------------
# Only these variables are captured from /proc/[PID]/environ during
# snapshotting. This prevents stale DBUS addresses, Wayland display tokens,
# and ephemeral session keys from a previous boot from leaking into
# restored sessions.
#
# Variables like WAYLAND_DISPLAY, DISPLAY, DBUS_SESSION_BUS_ADDRESS, and
# HYPRLAND_INSTANCE_SIGNATURE are intentionally excluded because they will
# be different on every boot and are inherited from the session naturally.

ENV_WHITELIST: FrozenSet[str] = frozenset({
    # Core identity
    "HOME",
    "USER",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",

    # Toolchain paths
    "PATH",
    "GOPATH",
    "GOROOT",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "VIRTUAL_ENV",
    "CONDA_DEFAULT_ENV",
    "CONDA_PREFIX",
    "NVM_DIR",
    "NODE_PATH",
    "PYENV_ROOT",

    # Development
    "EDITOR",
    "VISUAL",
    "PAGER",
    "TERM",
    "COLORTERM",

    # XDG base directories (user-controlled, stable across boots)
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
})


# ---------------------------------------------------------------------------
# Terminal Emulator Class Mappings
# ---------------------------------------------------------------------------
# Maps Hyprland window class names (from `hyprctl clients -j`) to their
# launch configurations. Each entry specifies:
#   - binary: the executable name
#   - cwd_flag: the CLI argument pattern for setting working directory
#   - is_terminal: True if the app spawns a child shell whose CWD we track

@dataclass(frozen=True, slots=True)
class AppProfile:
    """Launch profile for a supported application."""
    binary: str
    cwd_flag: str          # Python format string, e.g. "--directory={cwd}"
    is_terminal: bool      # Whether to introspect child shell processes
    shell_flag: str = ""   # Optional flag to specify a shell command to run


# Window class → AppProfile
# Hyprland reports `class` in lowercase; we normalize on lookup.
APP_PROFILES: dict[str, AppProfile] = {
    "ghostty": AppProfile(
        binary="ghostty",
        cwd_flag="--working-directory={cwd}",
        is_terminal=True,
    ),
    "kitty": AppProfile(
        binary="kitty",
        cwd_flag="--directory={cwd}",
        is_terminal=True,
    ),
    "foot": AppProfile(
        binary="foot",
        cwd_flag="-D {cwd}",
        is_terminal=True,
    ),
    "alacritty": AppProfile(
        binary="alacritty",
        cwd_flag="--working-directory={cwd}",
        is_terminal=True,
    ),
    "dev.zed.zed": AppProfile(
        binary="zed",
        cwd_flag="{cwd}",
        is_terminal=False,
    ),
    "zed": AppProfile(
        binary="zed",
        cwd_flag="{cwd}",
        is_terminal=False,
    ),
}


# ---------------------------------------------------------------------------
# Event Filtering
# ---------------------------------------------------------------------------
# Only these Hyprland IPC events trigger a state snapshot.
# The event stream format is: EVENT>>DATA\n

TRACKED_EVENTS: FrozenSet[str] = frozenset({
    "openwindow",
    "closewindow",
    "movewindow",
    "workspace",
    "focusedmon",
    "activewindowv2",
})


# ---------------------------------------------------------------------------
# Daemon Tuning
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """Operational parameters for the Hyprstate daemon."""

    # Debounce interval in seconds. After a tracked event fires, the daemon
    # waits this long for additional events before committing a snapshot.
    # Prevents excessive disk I/O during rapid window operations (e.g.
    # opening multiple terminals in quick succession).
    debounce_seconds: float = 0.5

    # Maximum number of seconds between periodic heartbeat snapshots,
    # even when no events fire. Acts as a safety net for CWD changes
    # that happen without Hyprland events (user cd-ing in a terminal).
    heartbeat_interval: float = 30.0

    # Socket read buffer size
    socket_buffer_size: int = 4096

    # Reconnect delay after socket disconnection (seconds)
    reconnect_delay: float = 2.0

    # Maximum reconnect attempts before giving up (0 = infinite)
    max_reconnect_attempts: int = 0


DAEMON_CONFIG = DaemonConfig()
