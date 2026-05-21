# Hyprstate 🚀

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform](https://img.shields.io/badge/platform-Arch%20Linux%20%7C%20Hyprland-blue)](https://wiki.hyprland.org/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

**Hyprstate** is a localized, low-overhead, event-driven session serialization and restoration daemon designed specifically for the **Hyprland** compositor (v0.55+) on **Arch Linux**.

Unlike standard session managers that simply reopen application binaries, Hyprstate preserves the internal context of your desktop workspaces. It tracks, persists, and automatically restores the **Current Working Directory (CWD)** and whitelisted **environment variables** of active terminal shell sessions.

---

## Key Features

- ⚡ **Zero-Overhead Event Loop**: Binds to Hyprland's IPC socket (`.socket2.sock`) to react instantaneously to window actions. Consumes **0% CPU** when idle.
- 🐚 **Deep Shell Context Tracking**: Extracts active CWDs and environment states from terminal child shells by traversing `/proc` trees. Out-of-the-box support for **Foot**, **Ghostty**, **Kitty**, and **Alacritty**.
- 🚦 **Race-Free Workspace Routing**: Routes restored applications using event-driven `movetoworkspacesilent` dispatch based on exact compositor window addresses immediately after mapping. Applications land on their correct workspaces silently without timing race conditions, class collisions, or rule leakage.
- 💾 **Crash-Resilient Atomic Writes**: Persists session manifests atomically using a `.tmp -> fsync -> rename -> fsync` parent-directory flush sequence. Guaranteed to prevent partial state corruption even under power failure.
- 🛡️ **Clean Environment Whitelisting**: Employs a strict **whitelist-only** environment block capture. Volatile and session-specific tokens (like `WAYLAND_DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`, and `HYPRLAND_INSTANCE_SIGNATURE`) are excluded to avoid stale environment pollution across system boots.

---

## How It Works

```
[ User Interaction ]
       │ (open, close, move, focus)
       ▼
[ Hyprland Compositor ] ──(socket2.sock)──► [ Hyprstate Daemon ]
                                                    │
                                                    ▼ (Introspects /proc)
                                            [ Resolve Shell Context ]
                                                    │ (CWD & Whitelisted envs)
                                                    ▼
                                            [ Atomic Writes ] ──► ~/.config/hyprstate/session.json
                                                                           │
                                                                           ▼ (system boot / login)
                                                                   [ Hyprstate Restorer ]
                                                                            │
                                                                            ▼ (movetoworkspacesilent dispatch)
                                                                 [ Perfect State Restored! ]
```

---

## Installation

### 1. Prerequisites
Hyprstate is optimized for **Arch Linux** running **Hyprland v0.55+** with Python 3.10+ installed.

### 2. Clone the Repository
Clone Hyprstate into your home directory:
```bash
git clone https://github.com/xCaptaiN09/Hyprstate.git ~/Projects/Hyprstate
```

### 3. Install Systemd User Services
Hyprstate provides systemd unit files to handle automatic serialization (in the background) and restoration (on session startup).

Copy or symlink the service files into your user systemd directory:
```bash
mkdir -p ~/.config/systemd/user/
ln -sf ~/Projects/Hyprstate/hyprstate.service ~/.config/systemd/user/hyprstate.service
ln -sf ~/Projects/Hyprstate/hyprstate-restore.service ~/.config/systemd/user/hyprstate-restore.service
```

### 4. Enable and Start the Services
Reload the systemd user daemon, then enable and start the serialization service:
```bash
# Reload user units
systemctl --user daemon-reload

# Start and enable the tracking daemon
systemctl --user enable --now hyprstate.service

# Enable the restore service to run automatically on session launch
systemctl --user enable hyprstate-restore.service
```

### 5. Hyprland Integration (`hyprland.conf`)

To ensure that systemd has access to the active Wayland session variables and that the compositor is fully initialized before restoration kicks in, integrate the following `exec-once` chain into your `hyprland.conf` (or your dedicated `execs.conf` startup file):

```ini
exec-once = systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP HYPRLAND_INSTANCE_SIGNATURE && dbus-update-activation-environment --systemd WAYLAND_DISPLAY XDG_CURRENT_DESKTOP HYPRLAND_INSTANCE_SIGNATURE && sleep 2 && systemctl --user start hyprstate-restore.service && systemctl --user start hyprstate.service
```

This sequence:
1. Imports modern desktop session variables to the systemd user environment.
2. Registers them with D-Bus.
3. Pauses for **2 seconds** to allow Hyprland to fully settle and start its socket listeners.
4. Triggers the restoration engine to perfectly route historical windows.
5. Launches the lightweight event tracking daemon.

*Note: The restore service runs once at graphical startup and terminates immediately after launching your applications.*

### 6. Uninstallation

To completely uninstall Hyprstate and restore your system configuration to default:

1. **Stop and disable the services**:
   ```bash
   systemctl --user disable --now hyprstate.service
   systemctl --user disable --now hyprstate-restore.service
   ```

2. **Remove the systemd unit files**:
   ```bash
   rm -f ~/.config/systemd/user/hyprstate.service
   rm -f ~/.config/systemd/user/hyprstate-restore.service
   systemctl --user daemon-reload
   ```

3. **Delete configuration and state directories**:
   ```bash
   rm -rf ~/.config/hyprstate/
   ```

4. **Clean up `hyprland.conf`**:
   Remove the `systemctl --user start hyprstate-restore.service` and `systemctl --user start hyprstate.service` commands from your `exec-once` chain in your `hyprland.conf` (or dedicated `execs.conf` file).

---

## Manual Usage

You can also run the daemon or restoration scripts manually from your terminal.

### Take a Manual State Snapshot
If the daemon is running, it saves state automatically on window movements. To manually run a serialization cycle or debug:
```bash
python3 daemon.py
```

### Trigger a Session Restoration
To restore the captured state manually:
```bash
python3 restore.py
```

### Dry-Run Restoration
To check what commands and environments would be executed during restoration without launching any processes:
```bash
python3 restore.py --dry-run
```

### Verbose Debugging
Enable detailed logs to standard error:
```bash
python3 restore.py --verbose
```

---

## Configuration

You can customize Hyprstate's behavior directly in `config.py`:

- **Environment Whitelist (`ENV_WHITELIST`)**: Specify which environment variables (e.g., `PATH`, `GOPATH`, `CARGO_HOME`, `EDITOR`, etc.) are allowed to survive across restoration sessions.
- **Application Profiles (`APP_PROFILES`)**: Map custom application classes (from `hyprctl clients -j`) to their launch binaries, flag structures, and terminal traits.
- **Daemon Tuning (`DaemonConfig`)**: Modify the debouncer interval (default `0.5s`) to throttle I/O during heavy window changes or adjust the heartbeat interval (default `30s`) to catch idle terminal shell state changes.

---

## Contributing and Feedback

Feedback is highly encouraged! Since Hyprstate interacts with active process trees and XDG sessions, testing across diverse desktop environments, shell setups (zsh, fish, bash), and terminal emulators will help perfect the software.

1. Fork this repository.
2. Create your feature branch (`git checkout -b feature/AmazingFeature`).
3. Run the automated test suite to ensure no regressions:
   ```bash
   python3 test_suite.py
   ```
4. Commit your changes and open a Pull Request.

---

## License

Distributed under the **MIT License**. See `LICENSE` for more information.
