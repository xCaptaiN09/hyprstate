"""
Hyprstate Restoration Engine — Session Re-hydration for Hyprland.

Reads the persisted session state from .session.json and reconstructs
the desktop environment:

    1. Reads the session manifest (windows, workspaces, shell contexts)
    2. Launches each application with CWD and environment restored
    3. Waits for the window to map via the compositor event socket
    4. Moves the window to its target workspace via dispatch

Workspace Routing Strategy:
    We listen for `openwindow>>ADDRESS,WS,CLASS,TITLE` events on
    Hyprland's .socket2.sock. When a restored window maps, we extract
    its compositor address and dispatch:
        hyprctl dispatch movetoworkspacesilent <ID>,address:<ADDR>
    This is more reliable than window rules because it uses the exact
    window handle — no regex matching, no rule leakage, no class
    collisions when restoring multiple windows of the same class.

Usage:
    python3 restore.py              # Restore from default state file
    python3 restore.py --dry-run    # Print what would be done without acting
    python3 restore.py --state /path/to/session.json  # Custom state file
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from config import (
    APP_PROFILES,
    STATE_FILE,
    _resolve_hyprland_socket_dir,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("hyprstate.restore")

_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_sh = logging.StreamHandler(sys.stderr)
_sh.setFormatter(_fmt)
_sh.setLevel(logging.INFO)
logger.addHandler(_sh)
logger.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# Hyprland IPC (shared command socket helper)
# ---------------------------------------------------------------------------

async def _hyprctl_command(socket_path: str, command: str) -> str:
    """
    Send a single command to the Hyprland command socket.

    Opens a fresh connection per command (Hyprland requires this).

    Args:
        socket_path: Full path to .socket.sock.
        command: The raw command string (e.g. "/keyword windowrule ...").

    Returns:
        Response string from Hyprland.
    """
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise ConnectionError(
            f"Cannot connect to Hyprland at {socket_path}: {exc}"
        ) from exc

    try:
        writer.write(command.encode("utf-8"))
        await writer.drain()

        chunks: list[bytes] = []
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            chunks.append(chunk)

        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# State File Reader
# ---------------------------------------------------------------------------

def load_session_state(state_path: Path) -> dict[str, Any]:
    """
    Read and validate the session state file.

    Args:
        state_path: Path to the session.json file.

    Returns:
        Parsed session state dictionary.

    Raises:
        FileNotFoundError: If the state file does not exist.
        json.JSONDecodeError: If the file is corrupt.
        ValueError: If the state format version is unsupported.
    """
    if not state_path.is_file():
        raise FileNotFoundError(f"Session state file not found: {state_path}")

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    version = state.get("version")
    if version != 1:
        raise ValueError(
            f"Unsupported session state version: {version} (expected 1)"
        )

    windows = state.get("windows", [])
    if not windows:
        logger.warning("Session state contains no windows")

    timestamp = state.get("timestamp_human", "unknown")
    logger.info(
        "Loaded session state: %d windows, saved at %s",
        len(windows),
        timestamp,
    )

    return state


# ---------------------------------------------------------------------------
# Window Mapping Event Listener
# ---------------------------------------------------------------------------

class WindowMapListener:
    """
    Listens to the Hyprland event socket (.socket2.sock) in the background
    to detect when spawned windows map to the compositor.

    Captures both the window address and class from openwindow events,
    allowing the restoration engine to move windows to their target
    workspaces using the exact compositor handle.
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.listeners: list[asyncio.Future[tuple[str, str]]] = []
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background event reading task."""
        self._task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        """Stop the background event reading task."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _read_loop(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
                try:
                    while True:
                        line_bytes = await reader.readline()
                        if not line_bytes:
                            break
                        line = line_bytes.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue

                        if line.startswith("openwindow>>"):
                            data = line[len("openwindow>>"):]
                            parts = data.split(",", 3)
                            if len(parts) >= 3:
                                win_address = parts[0]
                                win_class = parts[2]
                                # Wake up any pending futures with (address, class)
                                for fut in list(self.listeners):
                                    if not fut.done():
                                        fut.set_result((win_address, win_class))
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Error in event listener loop: %s", exc)
                await asyncio.sleep(0.5)

    async def wait_for_window(self, target_class: str, timeout: float = 8.0) -> str | None:
        """
        Wait for a window of the given class to map to the compositor.

        Args:
            target_class: Expected window class (e.g. 'foot', 'zen').
            timeout: Maximum seconds to wait before falling back.

        Returns:
            The window address (e.g. '55a139a9dfd0') if found, None on timeout.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[str, str]] = loop.create_future()
        self.listeners.append(fut)

        target_lower = target_class.lower()

        try:
            start_time = time.time()
            while time.time() - start_time < timeout:
                remaining = timeout - (time.time() - start_time)
                if remaining <= 0:
                    break
                try:
                    address, mapped_class = await asyncio.wait_for(asyncio.shield(fut), timeout=remaining)
                    if mapped_class.lower() == target_lower:
                        return address
                    else:
                        # Not the window class we want; keep waiting with a fresh future
                        if fut in self.listeners:
                            self.listeners.remove(fut)
                        fut = loop.create_future()
                        self.listeners.append(fut)
                except asyncio.TimeoutError:
                    break
            return None
        finally:
            if fut in self.listeners:
                self.listeners.remove(fut)


# ---------------------------------------------------------------------------
# Application Launcher
# ---------------------------------------------------------------------------

def _build_launch_command(
    window_entry: dict[str, Any],
) -> tuple[list[str], dict[str, str] | None]:
    """
    Build the shell command and environment for launching an application.

    For terminals: uses the app profile's CWD flag and restores the
    filtered environment from the session state.

    For non-terminals (e.g., Zed): launches with the project path.

    Args:
        window_entry: Window state dictionary from the session file.

    Returns:
        Tuple of (command_list, environment_dict_or_None).
        Returns ([], None) if the app cannot be launched.
    """
    profile_key = window_entry.get("profile_key")
    if not profile_key:
        # Generic fallback logic for unprofiled applications (e.g. browsers, file managers)
        window_class = window_entry.get("class", "")
        if not window_class:
            return [], None

        class_lower = window_class.lower()

        # Popular hardcoded fallbacks to handle class-to-binary mismatches
        generic_candidates = {
            # Web Browsers
            "zen": ["zen-browser", "zen"],
            "zen-alpha": ["zen-browser", "zen"],
            "google-chrome": ["google-chrome-stable", "google-chrome"],
            "google-chrome-unstable": ["google-chrome-unstable"],
            "chrome": ["google-chrome-stable", "google-chrome"],
            "firefox": ["firefox"],
            "firefox-developer-edition": ["firefox-developer-edition", "firefox"],
            "chromium": ["chromium"],
            "opera": ["opera"],
            "brave-browser": ["brave-browser", "brave"],
            "microsoft-edge-stable": ["microsoft-edge-stable", "microsoft-edge"],
            "microsoft-edge-dev": ["microsoft-edge-dev"],
            "vivaldi-stable": ["vivaldi-stable", "vivaldi"],

            # File Managers
            "thunar": ["thunar"],
            "org.gnome.nautilus": ["nautilus"],
            "nautilus": ["nautilus"],
            "pcmanfm": ["pcmanfm"],
            "pcmanfm-qt": ["pcmanfm-qt"],
            "dolphin": ["dolphin"],
            "nemo": ["nemo"],
            "doublecmd": ["doublecmd"],

            # Common desktop apps
            "code": ["code"],
            "code-oss": ["code-oss", "code"],
            "sublime_text": ["subl"],
            "spotify": ["spotify"],
            "discord": ["discord"],
            "steam": ["steam"],
            "mpv": ["mpv"],
            "vlc": ["vlc"],
            "gimp": ["gimp"],
            "blender": ["blender"],
            "obsidian": ["obsidian"],
            "slack": ["slack"],
            "telegram-desktop": ["telegram-desktop", "telegram"],
            "thunderbird": ["thunderbird"],
            "transmission-gtk": ["transmission-gtk"],
            "transmission-qt": ["transmission-qt"],
            "qbittorrent": ["qbittorrent"],
        }

        candidates = generic_candidates.get(class_lower, [class_lower, window_class])

        binary_path = None
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                binary_path = resolved
                break

        if not binary_path:
            logger.debug(
                "No candidate binary found in PATH for generic class: %s",
                window_class,
            )
            return [], None

        logger.info(
            "Resolved generic application class %s to binary %s",
            window_class,
            binary_path,
        )
        return [binary_path], None

    profile = APP_PROFILES.get(profile_key)
    if not profile:
        logger.warning("Unknown profile key: %s", profile_key)
        return [], None

    # Check that the binary exists
    binary_path = shutil.which(profile.binary)
    if not binary_path:
        logger.error(
            "Binary not found: %s (for window class %s)",
            profile.binary,
            profile_key,
        )
        return [], None

    cmd: list[str] = [binary_path]

    if profile.is_terminal:
        # Terminal — restore CWD via CLI flag
        context = window_entry.get("shell_context", {})
        cwd = context.get("cwd", os.path.expanduser("~"))

        # Verify the CWD still exists
        if not Path(cwd).is_dir():
            logger.warning(
                "Saved CWD no longer exists: %s — falling back to HOME",
                cwd,
            )
            cwd = os.path.expanduser("~")

        # Parse the CWD flag template
        cwd_arg = profile.cwd_flag.format(cwd=cwd)
        # Handle flags that are space-separated (e.g. foot's "-D /path")
        cmd.extend(cwd_arg.split())

        # Restore whitelisted environment variables
        saved_environ = context.get("environ", {})
        if saved_environ:
            # Start with current session environment (has valid DISPLAY,
            # WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS, etc.) and overlay
            # saved whitelisted variables on top.
            launch_env = os.environ.copy()
            for key, value in saved_environ.items():
                launch_env[key] = value
            return cmd, launch_env

    else:
        # Non-terminal (e.g., Zed) — launch with project path
        title_hint = window_entry.get("title_hint", "")
        context = window_entry.get("shell_context", {})
        cwd = context.get("cwd", "")

        if cwd and Path(cwd).is_dir():
            path_arg = profile.cwd_flag.format(cwd=cwd)
            cmd.extend(path_arg.split())
        elif title_hint:
            # Try to extract path from window title
            # Zed titles are typically "filename — Zed" or "project — Zed"
            parts = title_hint.split(" — ")
            if parts:
                potential_path = parts[0].strip()
                if Path(potential_path).exists():
                    path_arg = profile.cwd_flag.format(cwd=potential_path)
                    cmd.extend(path_arg.split())

    return cmd, None


async def launch_window(
    socket_path: str,
    window_entry: dict[str, Any],
    map_listener: WindowMapListener | None = None,
    dry_run: bool = False,
) -> bool:
    """
    Launch a single window and route it to the correct workspace.

    Protocol:
        1. Launch the application subprocess
        2. Wait for the openwindow event to get the compositor address
        3. Dispatch movetoworkspacesilent to move it to the target workspace

    Args:
        socket_path: Path to .socket.sock.
        window_entry: Window state dictionary.
        map_listener: Optional listener for detecting window map events.
        dry_run: If True, only log what would be done.

    Returns:
        True if the window was launched successfully.
    """
    window_class = window_entry.get("class", "")
    workspace_id = window_entry.get("workspace_id", 1)

    cmd, env = _build_launch_command(window_entry)
    if not cmd:
        logger.info(
            "Skipping unlaunchable window: class=%s, ws=%d",
            window_class,
            workspace_id,
        )
        return False

    cmd_str = " ".join(shlex.quote(c) for c in cmd)

    if dry_run:
        logger.info(
            "[DRY RUN] Would launch on workspace %d: %s",
            workspace_id,
            cmd_str,
        )
        return True

    # Step 1: Launch the application
    logger.info(
        "Launching on workspace %d: %s", workspace_id, cmd_str
    )

    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Detach from our process group
        )
        logger.debug("Spawned PID %d for %s", proc.pid, window_class)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("Failed to launch %s: %s", cmd_str, exc)
        return False

    # Step 2: Wait for the window to map and get its compositor address
    if map_listener and window_class:
        logger.debug("Waiting for window %s to map...", window_class)
        address = await map_listener.wait_for_window(window_class, timeout=8.0)
        if address:
            # Step 3: Move the window to its target workspace
            move_cmd = f"/dispatch movetoworkspacesilent {workspace_id},address:0x{address}"
            try:
                await _hyprctl_command(socket_path, move_cmd)
                logger.info(
                    "Moved window 0x%s (%s) to workspace %d",
                    address, window_class, workspace_id,
                )
            except ConnectionError as exc:
                logger.warning(
                    "Failed to move %s to workspace %d: %s",
                    window_class, workspace_id, exc,
                )
        else:
            logger.warning(
                "Timed out waiting for %s to map — window stays on active workspace",
                window_class,
            )
    else:
        await asyncio.sleep(1.5)

    return True


# ---------------------------------------------------------------------------
# Restoration Orchestrator
# ---------------------------------------------------------------------------

async def restore_session(
    state_path: Path,
    dry_run: bool = False,
) -> None:
    """
    Main restoration orchestrator.

    Reads the session state, groups windows by workspace, and launches
    them in order with proper workspace routing via window rules.

    Windows are launched sequentially within each workspace to allow
    Hyprland's layout engine (dwindle/master) to calculate split
    positions in the correct order.

    Args:
        state_path: Path to the session.json file.
        dry_run: If True, only log what would be done.
    """
    # Load state
    try:
        state = load_session_state(state_path)
    except FileNotFoundError:
        logger.error("No session state found at %s — nothing to restore", state_path)
        return
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Corrupt or invalid session state: %s", exc)
        return

    windows = state.get("windows", [])
    if not windows:
        logger.info("No windows to restore")
        return

    # Resolve Hyprland socket
    try:
        socket_dir = _resolve_hyprland_socket_dir()
    except RuntimeError as exc:
        logger.critical("Cannot find Hyprland session: %s", exc)
        return

    socket_path = str(socket_dir / ".socket.sock")

    # Verify Hyprland is responsive
    try:
        response = await _hyprctl_command(socket_path, "j/clients")
        logger.debug("Hyprland is responsive")
    except ConnectionError as exc:
        logger.critical("Hyprland is not responding: %s", exc)
        return

    # Filter to only launchable windows (either have a known profile, or can be resolved via generic fallback)
    launchable = []
    skipped = 0
    for w in windows:
        cmd, _ = _build_launch_command(w)
        if cmd:
            launchable.append(w)
        else:
            skipped += 1

    if skipped > 0:
        logger.info(
            "Skipping %d windows without launchable profiles or fallbacks", skipped
        )

    if not launchable:
        logger.info("No launchable windows in session state")
        return

    # Group by workspace and sort by workspace ID
    by_workspace: dict[int, list[dict[str, Any]]] = {}
    for w in launchable:
        ws_id = w.get("workspace_id", 1)
        by_workspace.setdefault(ws_id, []).append(w)

    total_launched = 0
    total_failed = 0

    # Start the dynamic map listener if not a dry run
    map_listener = None
    if not dry_run:
        socket2_path = str(socket_dir / ".socket2.sock")
        map_listener = WindowMapListener(socket2_path)
        await map_listener.start()

    try:
        # Launch workspace by workspace, windows sequentially within each
        for ws_id in sorted(by_workspace.keys()):
            ws_windows = by_workspace[ws_id]
            logger.info(
                "Restoring workspace %d (%d windows)", ws_id, len(ws_windows)
            )

            for window_entry in ws_windows:
                success = await launch_window(
                    socket_path, window_entry, map_listener=map_listener, dry_run=dry_run
                )
                if success:
                    total_launched += 1
                else:
                    total_failed += 1

                # Small delay between launches within the same workspace
                # to let the layout engine process each window
                if not dry_run:
                    await asyncio.sleep(0.5)
    finally:
        if map_listener:
            await map_listener.stop()

    logger.info(
        "Restoration complete: %d launched, %d failed, %d skipped",
        total_launched,
        total_failed,
        skipped,
    )


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        prog="hyprstate-restore",
        description="Restore Hyprland session state from a saved snapshot.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=STATE_FILE,
        help=f"Path to session state file (default: {STATE_FILE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without actually launching anything",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)
        _sh.setLevel(logging.DEBUG)

    try:
        asyncio.run(restore_session(args.state, dry_run=args.dry_run))
    except KeyboardInterrupt:
        logger.info("Restoration interrupted by user")


if __name__ == "__main__":
    main()
