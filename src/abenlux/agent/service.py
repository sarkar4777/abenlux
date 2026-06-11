"""
Run the on-device capture agent in the background, started automatically at user login, on Linux,
macOS, and Windows. The notification use-case forces a USER-level agent (not a root/system service):
toasts only render inside the developer's own GUI session, where DISPLAY / DBUS (Linux) and the
Aqua session (macOS) exist. So we install:

  * Linux   -> a systemd  --user  unit            (~/.config/systemd/user/abenlux-agent.service)
  * macOS   -> a launchd  LaunchAgent             (~/Library/LaunchAgents/com.abenlux.agent.plist)
  * Windows -> a Scheduled Task triggered ONLOGON (+ registers the toast AppUserModelID)

Config (ABEN_* / OTEL_*) is snapshotted at install time into ~/.abenlux/agent.env and re-loaded by
`abenlux agent run`, so the unit itself stays config-free and a developer can edit one file.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape


def _sd_quote(a: str) -> str:
    """quote one ExecStart argument for systemd. systemd is not a shell, but it honours double-quoted
    arguments with C-style escapes - so a launcher path containing a space stays one argument."""
    if a and all(c.isalnum() or c in "/._-=:@+" for c in a):
        return a
    return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'

APP_NAME = "Abenlux Agent"
LABEL = "com.abenlux.agent"                      # launchd label / systemd unit base
_DIR = Path.home() / ".abenlux"
ENV_FILE = _DIR / "agent.env"
_CONFIG_KEYS = ("ABEN_", "OTEL_")                # which env vars to snapshot for the agent


def _launch_argv() -> list[str]:
    """how to start the agent. prefer the installed console script, fall back to the interpreter."""
    exe = shutil.which("abenlux")
    if exe:
        return [exe, "agent", "run"]
    return [sys.executable, "-m", "abenlux.cli", "agent", "run"]


def _run(cmd: list[str]) -> bool:
    """run a service-manager command, tolerant of a missing binary (e.g. systemctl on a non-systemd
    box or in a container) - installing the unit file should never crash just because the manager
    isn't there to activate it. returns True only if the command ran and succeeded."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True).returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _run_out(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout or ""
    except (FileNotFoundError, OSError):
        return ""


def write_env_file() -> Path:
    """snapshot the current ABEN_*/OTEL_* environment so the agent starts with the same config."""
    _DIR.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in sorted(os.environ.items())
             if k.startswith(_CONFIG_KEYS) and v != ""]
    ENV_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return ENV_FILE


def load_env_pairs() -> dict[str, str]:
    """parse ~/.abenlux/agent.env into a dict (no side effects)."""
    out: dict[str, str] = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def load_env_file() -> int:
    """load ~/.abenlux/agent.env into os.environ. MUST run before the gateway module is imported,
    since Settings reads the environment once at import. returns how many vars were applied."""
    pairs = load_env_pairs()
    for k, v in pairs.items():
        os.environ.setdefault(k, v)  # an explicit env var wins over the file
    return len(pairs)


# ----------------------------------------------------------------- Linux (systemd --user) ----
def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{LABEL}.service"


def _install_linux(port: int) -> str:
    unit = _systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    argv = " ".join(_sd_quote(a) for a in _launch_argv() + ["--port", str(port)])
    unit.write_text(
        f"[Unit]\nDescription={APP_NAME}\nAfter=network-online.target\n\n"
        f"[Service]\nType=simple\nEnvironmentFile=-{ENV_FILE}\nExecStart={argv}\n"
        "Restart=on-failure\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n", encoding="utf-8")
    _run(["systemctl", "--user", "daemon-reload"])
    started = _run(["systemctl", "--user", "enable", "--now", f"{LABEL}.service"])
    tail = "and started it" if started else "(run `systemctl --user enable --now abenlux-agent` once systemd is up)"
    return f"installed systemd --user unit at {unit} {tail}"


def _status_linux() -> str:
    if not _systemd_unit_path().exists():
        return "systemd --user: not-installed"
    return f"systemd --user: {_run_out(['systemctl', '--user', 'is-active', f'{LABEL}.service']).strip() or 'installed'}"


def _uninstall_linux() -> str:
    _run(["systemctl", "--user", "disable", "--now", f"{LABEL}.service"])
    _systemd_unit_path().unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])
    return "removed systemd --user unit"


# ----------------------------------------------------------------- macOS (launchd) -----------
def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _install_macos(port: int) -> str:
    plist = _plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    args = _launch_argv() + ["--port", str(port)]
    prog = "".join(f"        <string>{_xml_escape(a)}</string>\n" for a in args)
    env = load_env_pairs()
    envxml = "".join(f"        <key>{_xml_escape(k)}</key><string>{_xml_escape(v)}</string>\n"
                     for k, v in env.items())
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f'    <key>Label</key><string>{LABEL}</string>\n'
        f'    <key>ProgramArguments</key><array>\n{prog}    </array>\n'
        f'    <key>EnvironmentVariables</key><dict>\n{envxml}    </dict>\n'
        '    <key>RunAtLoad</key><true/>\n    <key>KeepAlive</key><true/>\n'
        '</dict></plist>\n', encoding="utf-8")
    _run(["launchctl", "unload", str(plist)])
    loaded = _run(["launchctl", "load", "-w", str(plist)])
    return f"installed launchd agent at {plist} {'and loaded it' if loaded else '(load it with launchctl)'}"


def _status_macos() -> str:
    if not _plist_path().exists():
        return "launchd: not-installed"
    return f"launchd: {'loaded' if LABEL in _run_out(['launchctl', 'list']) else 'installed (not loaded)'}"


def _uninstall_macos() -> str:
    _run(["launchctl", "unload", "-w", str(_plist_path())])
    _plist_path().unlink(missing_ok=True)
    return "removed launchd agent"


# ----------------------------------------------------------------- Windows (Startup folder) --
# schtasks /Create needs rights a locked-down/managed machine often denies a standard user, so we use
# the per-user Startup folder: a hidden VBS launcher that runs at login, in the user's session (where
# toasts render), with no admin and no console window. The toast AppUserModelID is registered too.
def _startup_dir() -> Path:
    roaming = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(roaming) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _win_launcher() -> Path:
    return _startup_dir() / "AbenluxAgent.vbs"


def _install_windows(port: int) -> str:
    from abenlux.developer.notify import register_windows_aumid
    register_windows_aumid()                                  # so toasts actually appear
    argv = _launch_argv() + ["--port", str(port)]
    inner = " ".join(f'""{a}""' if " " in a else a for a in argv)  # double-quotes for paths with spaces
    p = _win_launcher()
    p.parent.mkdir(parents=True, exist_ok=True)
    # window style 0 = hidden, False = don't wait. runs the agent at every login, no console flash.
    p.write_text(f'CreateObject("WScript.Shell").Run "{inner}", 0, False\r\n', encoding="utf-8")
    _run(["wscript", str(p)])  # start it now for this session too
    return f"installed Windows login launcher at {p} and registered the toast AUMID"


def _status_windows() -> str:
    return f"login launcher: {'installed' if _win_launcher().exists() else 'not-installed'}"


def _uninstall_windows() -> str:
    _win_launcher().unlink(missing_ok=True)
    return "removed Windows login launcher"


# ----------------------------------------------------------------- dispatch ------------------
def _wire_rtk() -> str:
    # RTK (Rust Token Killer, rtk-ai/rtk) compresses command OUTPUT at the agent's tool-hook layer,
    # below abenlux. they stack: RTK shrinks tool output before it enters the prompt, abenlux measures
    # and attributes the result. if rtk is installed we wire its hook; otherwise we point the dev to it.
    if shutil.which("rtk") is None:
        return " RTK not found: install it (https://github.com/rtk-ai/rtk) and re-run, or `rtk init -g`, to also compress command output."
    try:
        subprocess.run(["rtk", "init", "-g"], capture_output=True, timeout=30, check=False)
        return " RTK detected and its tool hook wired (`rtk init -g`): command output is now compressed too."
    except Exception:
        return " RTK detected but `rtk init -g` did not complete; run it manually to compress command output."


def install(port: int = 8088) -> str:
    write_env_file()
    sysname = platform.system()
    if sysname == "Linux":
        base = _install_linux(port)
    elif sysname == "Darwin":
        base = _install_macos(port)
    elif sysname == "Windows":
        base = _install_windows(port)
    else:
        return f"unsupported platform: {sysname}"
    return base + "\n" + _wire_rtk()


AGENT_LOG = _DIR / "agent.log"


def agent_alive(port: int = 8088) -> bool:
    # actually probe the capture process, not just whether the install artifact exists. Windows has no
    # service manager reporting liveness, so the install-state alone can read 'installed' while the
    # agent died at launch (bad port / bad config) - this catches that.
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as r:  # noqa: S310
            return r.status == 200
    except Exception:
        return False


def log_agent_crash(returncode: int, port: int) -> None:
    # append a timestamped crash line so a silently-restarting agent is diagnosable (esp. on Windows,
    # whose fire-and-forget launcher has no console to surface the error).
    from datetime import datetime, timezone
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        with open(AGENT_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()} gateway exited rc={returncode} port={port}\n")
    except Exception:
        pass


def status() -> str:
    sysname = platform.system()
    base = {"Linux": _status_linux, "Darwin": _status_macos, "Windows": _status_windows}.get(
        sysname, lambda: f"unsupported platform: {sysname}")()
    port = int(os.getenv("ABEN_AGENT_PORT", "8088"))
    live = "running (health OK)" if agent_alive(port) else f"NOT responding on :{port}"
    return f"{base}\n capture process: {live}"


def uninstall() -> str:
    sysname = platform.system()
    return {"Linux": _uninstall_linux, "Darwin": _uninstall_macos, "Windows": _uninstall_windows}.get(
        sysname, lambda: f"unsupported platform: {sysname}")()
