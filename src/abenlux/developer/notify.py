"""
Ambient developer notifications. The whole point of the waste/collaboration/budget signals is
that they reach the developer where they already are, not in a dashboard they have to remember to
open. So the edge agent raises a native OS notification the moment a meaningful signal fires.

Cross-platform, best-effort, dependency-free. Windows uses a built-in WinRT toast, macOS uses
osascript, Linux uses notify-send. Anything missing degrades to silence (the signal is still in
the local feed and `abenlux watch`/`abenlux me`). Never blocks a capture and never raises.

A debounce keeps it from being noisy: at most one toast per signal-kind per cooldown window, so a
flurry of identical retries is one heads-up, not ten.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import time

_TITLE = "Abenlux"
# a stable AppUserModelID. Win10/11 drop toasts from an app whose AUMID is not registered, so the
# agent installer writes a registry entry for this id (register_windows_aumid) and the toast is
# raised under it. Without registration the toast may silently no-show - hence `abenlux agent install`.
_AUMID = "Abenlux.Agent"


def register_windows_aumid() -> bool:
    """register the AppUserModelID under HKCU so Windows reliably shows our toasts. idempotent,
    best-effort, returns True if it ran. invoked by `abenlux agent install`, not on every toast."""
    if platform.system() != "Windows":
        return False
    ps = (
        f"$k='HKCU:\\SOFTWARE\\Classes\\AppUserModelId\\{_AUMID}';"
        "New-Item -Path $k -Force | Out-Null;"
        f"New-ItemProperty -Path $k -Name DisplayName -Value '{_TITLE}' -PropertyType String -Force | Out-Null"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _windows(message: str) -> None:
    # built-in WinRT toast, no install needed on Win10/11, raised under our registered AUMID
    msg = message.replace("'", "`'")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
        "ContentType=WindowsRuntime]|Out-Null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x=$t.GetElementsByTagName('text');"
        f"$x.Item(0).AppendChild($t.CreateTextNode('{_TITLE}'))|Out-Null;"
        f"$x.Item(1).AppendChild($t.CreateTextNode('{msg}'))|Out-Null;"
        "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_AUMID}').Show($n)"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, timeout=5)


def _macos(message: str) -> None:
    msg = message.replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{_TITLE}"'],
                   capture_output=True, timeout=5)


def _linux(message: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", _TITLE, _TITLE, message], capture_output=True, timeout=5)


def notify(message: str) -> bool:
    """raise a native desktop notification. returns True if a backend was invoked, else False.
    best-effort: a failure or missing backend is swallowed, the signal stays in the feed."""
    try:
        system = platform.system()
        if system == "Windows":
            _windows(message)
        elif system == "Darwin":
            _macos(message)
        elif system == "Linux":
            _linux(message)
        else:
            return False
        return True
    except Exception:
        return False


class Debouncer:
    """one notification per kind per cooldown window, so identical signals don't spam."""

    def __init__(self, cooldown_s: float = 30.0, clock=time.monotonic):
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._last: dict[str, float] = {}

    def allow(self, kind: str) -> bool:
        now = self._clock()
        last = self._last.get(kind)
        if last is not None and (now - last) < self.cooldown_s:
            return False
        self._last[kind] = now
        return True
