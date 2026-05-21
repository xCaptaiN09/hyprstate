"""
Hyprstate Daemon — Event-Driven Session Serializer for Hyprland.

Connects to the Hyprland IPC event socket (.socket2.sock), listens for
state-changing window events, performs process introspection via /proc,
and atomically persists the session state to disk.

Architecture:
    1. asyncio event loop binds to .socket2.sock (UNIX domain socket)
    2. Events are debounced to prevent excessive I/O during bursts
    3. On snapshot: query hyprctl clients via .socket.sock, traverse
       /proc to resolve shell CWDs and environments
    4. State is written atomically: write to .tmp → fsync → rename

Critical design decisions:
    - Process introspection uses /proc/[PID]/status (PPid: key-value)
      instead of /proc/[PID]/stat (space-delimited) to avoid field-
      shifting bugs when process names contain spaces.
    - Environment capture uses a strict whitelist, never a blacklist.
    - The daemon consumes 0% CPU when idle (blocked on socket read).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from config import (
    APP_PROFILES,
    DAEMON_CONFIG,
    ENV_WHITELIST,
    LOG_FILE,
    STATE_DIR,
    STATE_FILE,
    STATE_TMP_FILE,
    TRACKED_EVENTS,
    _resolve_hyprland_socket_dir,
)

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("hyprstate")


def _setup_logging() -> None:
    """Configure logging to both stderr and the log file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    # Stderr handler (for systemd journal capture)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)

    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# Hyprland IPC — Command Socket (.socket.sock)
# ---------------------------------------------------------------------------

class HyprlandIPC:
    """
    Synchronous helper for querying the Hyprland command socket.

    Uses a fresh connection per query (Hyprland closes the socket after
    each response). The protocol is:
        - Send: "j/<command>" for JSON output
        - Receive: raw JSON until EOF
    """

    def __init__(self, socket_dir: Path) -> None:
        self._cmd_sock_path = str(socket_dir / ".socket.sock")

    async def query_json(self, command: str) -> Any:
        """
        Send a query to the Hyprland command socket and parse JSON response.

        Args:
            command: The Hyprland IPC command (e.g. "clients", "workspaces").
                     Will be prefixed with "j/" for JSON output.

        Returns:
            Parsed JSON data (list or dict).

        Raises:
            ConnectionError: If the socket is unreachable.
            json.JSONDecodeError: If the response is not valid JSON.
        """
        try:
            reader, writer = await asyncio.open_unix_connection(
                self._cmd_sock_path
            )
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise ConnectionError(
                f"Cannot connect to Hyprland command socket "
                f"at {self._cmd_sock_path}: {exc}"
            ) from exc

        try:
            writer.write(f"j/{command}".encode("utf-8"))
            await writer.drain()

            chunks: list[bytes] = []
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)

            raw = b"".join(chunks)
            return json.loads(raw)

        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass  # Socket may already be closed by Hyprland

    async def dispatch(self, command: str) -> str:
        """
        Send a dispatch command to Hyprland (non-JSON).

        Args:
            command: Full dispatch string, e.g.
                     "keyword windowrulev2 workspace 3 silent, class:^foot$"

        Returns:
            Raw response string from Hyprland.
        """
        try:
            reader, writer = await asyncio.open_unix_connection(
                self._cmd_sock_path
            )
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise ConnectionError(
                f"Cannot connect to Hyprland command socket: {exc}"
            ) from exc

        try:
            writer.write(f"/{command}".encode("utf-8"))
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
# Process Introspection via /proc
# ---------------------------------------------------------------------------

def _read_proc_status_field(pid: int, field_name: str) -> str | None:
    """
    Read a specific field from /proc/[pid]/status.

    Uses line-by-line key-value parsing instead of positional splitting
    of /proc/[pid]/stat. This is immune to processes with spaces,
    parentheses, or special characters in their command names.

    Args:
        pid: Process ID.
        field_name: Field name including colon, e.g. "PPid:".

    Returns:
        The field value as a stripped string, or None if the process
        or field does not exist.
    """
    status_path = Path(f"/proc/{pid}/status")
    try:
        with open(status_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(field_name):
                    # Format is "PPid:\t<value>\n"
                    return line.split(":", 1)[1].strip()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None

    return None


def _get_child_pids(parent_pid: int) -> list[int]:
    """
    Find all direct child PIDs of a given parent by scanning /proc.

    Reads /proc/[PID]/status for each process and checks the PPid field.
    This is the safe approach — no string splitting on /proc/[PID]/stat.

    Args:
        parent_pid: The parent process ID.

    Returns:
        List of child PIDs (may be empty).
    """
    children: list[int] = []

    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        logger.error("Cannot read /proc directory")
        return children

    for entry in proc_entries:
        if not entry.isdigit():
            continue

        pid = int(entry)
        ppid_str = _read_proc_status_field(pid, "PPid:")
        if ppid_str is None:
            continue

        try:
            if int(ppid_str) == parent_pid:
                children.append(pid)
        except ValueError:
            # Malformed PPid value — skip
            continue

    return children


def _find_shell_pid(terminal_pid: int) -> int | None:
    """
    Given a terminal emulator's PID, find the direct child shell process.

    Traverses one level down the process tree. If the terminal has multiple
    children (e.g. from terminal splits), we pick the first one that looks
    like a shell. If no shell-like child is found, we return the first
    child (best effort).

    Args:
        terminal_pid: PID of the terminal emulator (e.g. foot, ghostty).

    Returns:
        PID of the child shell process, or None if no children exist.
    """
    children = _get_child_pids(terminal_pid)
    if not children:
        return None

    # Known shell basenames
    shell_names = {"bash", "zsh", "fish", "sh", "dash", "tcsh", "csh", "nu"}

    for child_pid in children:
        name = _read_proc_status_field(child_pid, "Name:")
        if name and name.lower() in shell_names:
            return child_pid

    # Fallback: return first child (might be tmux, screen, etc.)
    return children[0]


def _read_cwd(pid: int) -> str | None:
    """
    Read the current working directory of a process via /proc/[pid]/cwd.

    Args:
        pid: Process ID.

    Returns:
        Absolute path string, or None if unreadable.
    """
    cwd_link = Path(f"/proc/{pid}/cwd")
    try:
        resolved = cwd_link.resolve(strict=True)
        return str(resolved)
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _read_environ(pid: int) -> dict[str, str]:
    """
    Read and filter environment variables from /proc/[pid]/environ.

    Only variables present in ENV_WHITELIST are retained. This prevents
    stale session tokens (DBUS addresses, Wayland display names, etc.)
    from contaminating restored sessions on subsequent boots.

    The environ file contains null-byte separated KEY=VALUE pairs.

    Args:
        pid: Process ID.

    Returns:
        Dictionary of whitelisted environment variables.
    """
    environ_path = Path(f"/proc/{pid}/environ")
    filtered: dict[str, str] = {}

    try:
        with open(environ_path, "rb") as f:
            raw = f.read()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return filtered

    for entry in raw.split(b"\x00"):
        if not entry:
            continue

        try:
            decoded = entry.decode("utf-8", errors="replace")
        except Exception:
            continue

        eq_idx = decoded.find("=")
        if eq_idx <= 0:
            continue

        key = decoded[:eq_idx]
        if key in ENV_WHITELIST:
            filtered[key] = decoded[eq_idx + 1 :]

    return filtered


def get_shell_context(terminal_pid: int) -> dict[str, Any] | None:
    """
    Extract the full shell context for a terminal emulator process.

    Finds the child shell, reads its CWD and filtered environment.

    Args:
        terminal_pid: PID of the terminal emulator window.

    Returns:
        Dictionary with keys: shell_pid, cwd, environ, shell_name.
        Returns None if the process tree cannot be resolved.
    """
    shell_pid = _find_shell_pid(terminal_pid)
    if shell_pid is None:
        logger.debug(
            "No child shell found for terminal PID %d", terminal_pid
        )
        return None

    cwd = _read_cwd(shell_pid)
    if cwd is None:
        logger.debug("Cannot read CWD for shell PID %d", shell_pid)
        return None

    environ = _read_environ(shell_pid)
    shell_name = _read_proc_status_field(shell_pid, "Name:") or "unknown"

    return {
        "shell_pid": shell_pid,
        "cwd": cwd,
        "environ": environ,
        "shell_name": shell_name,
    }


# ---------------------------------------------------------------------------
# Snapshot Builder
# ---------------------------------------------------------------------------

async def build_snapshot(ipc: HyprlandIPC) -> dict[str, Any]:
    """
    Build a complete session snapshot from Hyprland state + /proc.

    Queries hyprctl for all clients and workspaces, then enriches
    terminal windows with shell context from /proc.

    Args:
        ipc: HyprlandIPC instance for querying the compositor.

    Returns:
        Session state dictionary ready for JSON serialization.
    """
    try:
        clients = await ipc.query_json("clients")
    except (ConnectionError, json.JSONDecodeError) as exc:
        logger.error("Failed to query Hyprland clients: %s", exc)
        return {}

    try:
        workspaces = await ipc.query_json("workspaces")
    except (ConnectionError, json.JSONDecodeError) as exc:
        logger.warning("Failed to query workspaces: %s", exc)
        workspaces = []

    try:
        monitors = await ipc.query_json("monitors")
    except (ConnectionError, json.JSONDecodeError) as exc:
        logger.warning("Failed to query monitors: %s", exc)
        monitors = []

    snapshot_windows: list[dict[str, Any]] = []

    for client in clients:
        window_class = client.get("class", "").lower()
        workspace_id = client.get("workspace", {}).get("id", -1)
        window_pid = client.get("pid", 0)
        address = client.get("address", "")
        title = client.get("title", "")
        floating = client.get("floating", False)
        position = client.get("at", [0, 0])
        size = client.get("size", [0, 0])
        monitor_id = client.get("monitor", 0)

        # Skip special workspaces (negative IDs) and unmapped windows
        if workspace_id < 0:
            continue

        if not client.get("mapped", True):
            continue

        window_entry: dict[str, Any] = {
            "address": address,
            "class": window_class,
            "title": title,
            "pid": window_pid,
            "workspace_id": workspace_id,
            "monitor": monitor_id,
            "floating": floating,
            "position": position,
            "size": size,
        }

        # Check if this is a known/supported application
        profile = APP_PROFILES.get(window_class)
        if profile is not None:
            window_entry["profile_key"] = window_class

            if profile.is_terminal and window_pid > 0:
                # Extract shell context via /proc
                context = get_shell_context(window_pid)
                if context is not None:
                    window_entry["shell_context"] = context
                    logger.debug(
                        "Captured CWD for %s (PID %d): %s",
                        window_class,
                        window_pid,
                        context["cwd"],
                    )
                else:
                    logger.debug(
                        "No shell context for %s (PID %d)",
                        window_class,
                        window_pid,
                    )
            elif not profile.is_terminal:
                # For non-terminal apps like Zed, use the window title
                # as a hint for the project directory
                window_entry["title_hint"] = title
        else:
            # Untracked application — store metadata only for layout
            window_entry["profile_key"] = None

        snapshot_windows.append(window_entry)

    # Sort by workspace ID, then by focus history for deterministic
    # sequential restoration order
    snapshot_windows.sort(
        key=lambda w: (
            w["workspace_id"],
            w.get("_focus_order", 999),
        )
    )

    snapshot: dict[str, Any] = {
        "version": 1,
        "timestamp": time.time(),
        "timestamp_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workspaces": [
            {
                "id": ws.get("id"),
                "name": ws.get("name", ""),
                "monitor": ws.get("monitor", ""),
                "window_count": ws.get("windows", 0),
            }
            for ws in workspaces
            if ws.get("id", -1) >= 0  # Exclude special workspaces
        ],
        "monitors": [
            {
                "id": mon.get("id"),
                "name": mon.get("name", ""),
                "width": mon.get("width", 0),
                "height": mon.get("height", 0),
                "active_workspace": mon.get("activeWorkspace", {}).get(
                    "id", 1
                ),
            }
            for mon in monitors
        ],
        "windows": snapshot_windows,
    }

    return snapshot


# ---------------------------------------------------------------------------
# Atomic State Writer
# ---------------------------------------------------------------------------

def write_state_atomic(snapshot: dict[str, Any]) -> None:
    """
    Persist the session state to disk using an atomic write protocol.

    Protocol:
        1. Serialize to JSON
        2. Write to a temporary file (.session.json.tmp)
        3. Flush the Python buffer
        4. fsync the file descriptor to force kernel buffer → disk
        5. Close the file
        6. Atomically rename .tmp → .json (single syscall, no partial state)
        7. fsync the parent directory to ensure the rename is durable

    This guarantees that even under sudden power loss or kernel panic,
    the session file is either the complete previous state or the
    complete new state — never a partial/corrupt mix.

    Args:
        snapshot: The session state dictionary to persist.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(snapshot, indent=2, ensure_ascii=False)
    payload_bytes = payload.encode("utf-8")

    try:
        # Step 1: Write to temporary file
        fd = os.open(
            str(STATE_TMP_FILE),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            written = 0
            while written < len(payload_bytes):
                written += os.write(fd, payload_bytes[written:])

            # Step 2: Force data to physical storage
            os.fsync(fd)

        finally:
            os.close(fd)

        # Step 3: Atomic rename (POSIX guarantees this is atomic on
        # the same filesystem)
        os.rename(str(STATE_TMP_FILE), str(STATE_FILE))

        # Step 4: Fsync the directory to ensure the rename metadata
        # is durable
        dir_fd = os.open(str(STATE_DIR), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        logger.debug(
            "State written: %d bytes, %d windows",
            len(payload_bytes),
            len(snapshot.get("windows", [])),
        )

    except OSError as exc:
        logger.error(
            "Failed to write state file: %s (errno=%s)", exc, exc.errno
        )
        # Clean up the temp file if it exists
        try:
            STATE_TMP_FILE.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Event Socket Listener
# ---------------------------------------------------------------------------

async def _listen_events(
    socket_dir: Path,
    snapshot_trigger: asyncio.Event,
) -> None:
    """
    Connect to the Hyprland event socket and signal snapshot triggers.

    The event socket emits lines in the format: EVENT>>DATA\n
    We filter for events in TRACKED_EVENTS and set the snapshot_trigger
    asyncio.Event to wake the debounce loop.

    This coroutine will reconnect automatically if the socket drops.

    Args:
        socket_dir: Path to the Hyprland socket directory.
        snapshot_trigger: Event flag to signal the snapshot debouncer.
    """
    event_sock_path = str(socket_dir / ".socket2.sock")
    attempt = 0

    while True:
        try:
            logger.info("Connecting to event socket: %s", event_sock_path)
            reader, writer = await asyncio.open_unix_connection(
                event_sock_path
            )
            logger.info("Connected to Hyprland event socket")
            attempt = 0  # Reset on successful connection

            while True:
                line_bytes = await reader.readline()
                if not line_bytes:
                    # EOF — Hyprland closed the connection (compositor exit)
                    logger.warning("Event socket EOF — compositor exited?")
                    break

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                # Parse event: "EVENT>>DATA"
                separator_idx = line.find(">>")
                if separator_idx < 0:
                    logger.debug("Malformed event line: %r", line)
                    continue

                event_name = line[:separator_idx]

                if event_name in TRACKED_EVENTS:
                    logger.debug("Tracked event: %s", line)
                    snapshot_trigger.set()
                else:
                    logger.debug("Ignored event: %s", event_name)

        except (FileNotFoundError, ConnectionRefusedError) as exc:
            logger.warning("Event socket unavailable: %s", exc)

        except asyncio.CancelledError:
            logger.info("Event listener cancelled")
            return

        except Exception as exc:
            logger.error(
                "Unexpected error in event listener: %s", exc, exc_info=True
            )

        # Reconnection logic
        max_attempts = DAEMON_CONFIG.max_reconnect_attempts
        if max_attempts > 0:
            attempt += 1
            if attempt > max_attempts:
                logger.error(
                    "Max reconnect attempts (%d) exceeded. Shutting down.",
                    max_attempts,
                )
                return

        delay = DAEMON_CONFIG.reconnect_delay
        logger.info("Reconnecting in %.1fs (attempt %d)...", delay, attempt)
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Debounced Snapshot Loop
# ---------------------------------------------------------------------------

async def _snapshot_loop(
    ipc: HyprlandIPC,
    snapshot_trigger: asyncio.Event,
) -> None:
    """
    Debounced snapshot writer.

    Waits for the snapshot_trigger event, then waits an additional
    debounce period for event bursts to settle. Also performs periodic
    heartbeat snapshots to catch CWD changes that happen without
    Hyprland events.

    Args:
        ipc: HyprlandIPC instance for querying the compositor.
        snapshot_trigger: Event flag set by the event listener.
    """
    debounce = DAEMON_CONFIG.debounce_seconds
    heartbeat = DAEMON_CONFIG.heartbeat_interval

    while True:
        try:
            # Wait for either a triggered event or the heartbeat timeout
            try:
                await asyncio.wait_for(
                    snapshot_trigger.wait(), timeout=heartbeat
                )
                # Event was triggered — debounce
                snapshot_trigger.clear()
                await asyncio.sleep(debounce)

                # Drain any additional triggers that fired during debounce
                snapshot_trigger.clear()

                logger.debug("Snapshot triggered by event (debounced)")

            except asyncio.TimeoutError:
                # Heartbeat — take a snapshot anyway
                logger.debug("Heartbeat snapshot (no events in %.0fs)", heartbeat)

            # Build and write the snapshot
            snapshot = await build_snapshot(ipc)
            if snapshot and snapshot.get("windows"):
                write_state_atomic(snapshot)
            elif snapshot:
                logger.debug("Snapshot has no windows — skipping write")

        except asyncio.CancelledError:
            # Final snapshot before shutdown
            logger.info("Snapshot loop cancelled — writing final state")
            try:
                final = await build_snapshot(ipc)
                if final and final.get("windows"):
                    write_state_atomic(final)
                    logger.info("Final state written successfully")
            except Exception as exc:
                logger.error("Failed to write final state: %s", exc)
            return

        except Exception as exc:
            logger.error(
                "Error in snapshot loop: %s", exc, exc_info=True
            )
            await asyncio.sleep(1.0)  # Prevent tight error loops


# ---------------------------------------------------------------------------
# Main Daemon Entry Point
# ---------------------------------------------------------------------------

async def run_daemon() -> None:
    """
    Main daemon coroutine.

    Sets up signal handlers, resolves socket paths, and launches the
    event listener and snapshot loop as concurrent tasks.
    """
    _setup_logging()
    logger.info("Hyprstate daemon starting")

    # Resolve Hyprland socket directory
    try:
        socket_dir = _resolve_hyprland_socket_dir()
    except RuntimeError as exc:
        logger.critical("Startup failed: %s", exc)
        sys.exit(1)

    logger.info("Hyprland socket directory: %s", socket_dir)

    ipc = HyprlandIPC(socket_dir)
    snapshot_trigger = asyncio.Event()

    # Verify connectivity with an initial query
    try:
        clients = await ipc.query_json("clients")
        logger.info(
            "Initial Hyprland query successful: %d clients", len(clients)
        )
    except ConnectionError as exc:
        logger.critical("Cannot reach Hyprland IPC: %s", exc)
        sys.exit(1)

    # Setup graceful shutdown via signals
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler(signum: int) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — initiating graceful shutdown", sig_name)
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler, sig)

    # Launch concurrent tasks
    listener_task = asyncio.create_task(
        _listen_events(socket_dir, snapshot_trigger),
        name="event-listener",
    )
    snapshot_task = asyncio.create_task(
        _snapshot_loop(ipc, snapshot_trigger),
        name="snapshot-loop",
    )

    # Take an immediate initial snapshot
    snapshot_trigger.set()

    logger.info("Hyprstate daemon running — waiting for events")

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Cancel tasks gracefully
    logger.info("Shutting down tasks...")
    listener_task.cancel()
    snapshot_task.cancel()

    await asyncio.gather(listener_task, snapshot_task, return_exceptions=True)

    logger.info("Hyprstate daemon stopped")


def main() -> None:
    """CLI entry point."""
    try:
        asyncio.run(run_daemon())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
