"""
Ambient developer notifications. The signal has to reach the developer where they work, not in a
dashboard they have to open. These cover the debounce (no spam), the best-effort cross-platform
behavior, and that the gateway only toasts actionable signals and respects the debounce.
"""
from abenlux.developer.notify import Debouncer, notify


def test_debouncer_one_per_kind_per_window():
    t = {"v": 0.0}
    d = Debouncer(cooldown_s=30.0, clock=lambda: t["v"])
    assert d.allow("retry_loop") is True
    assert d.allow("retry_loop") is False          # same kind, within cooldown
    assert d.allow("budget_guardrail") is True     # different kind is independent
    t["v"] = 31.0
    assert d.allow("retry_loop") is True            # cooldown elapsed


def test_notify_unknown_platform_is_silent_not_error(monkeypatch):
    import abenlux.developer.notify as n
    monkeypatch.setattr(n.platform, "system", lambda: "Plan9")
    assert notify("hello") is False                 # no backend, but never raises


def test_notify_never_raises_on_backend_failure(monkeypatch):
    import abenlux.developer.notify as n
    monkeypatch.setattr(n.platform, "system", lambda: "Linux")
    monkeypatch.setattr(n.shutil, "which", lambda _: "/usr/bin/notify-send")

    def boom(*a, **k):
        raise OSError("no display")

    monkeypatch.setattr(n.subprocess, "run", boom)
    assert notify("hello") is False                 # swallowed, signal still lives in the feed


def test_gateway_toasts_only_warnings_and_debounces(monkeypatch):
    from abenlux.capture import gateway
    calls = []
    monkeypatch.setattr(gateway, "_NOTIFY", True)
    monkeypatch.setattr(gateway, "_debounce", Debouncer(cooldown_s=999, clock=lambda: 0.0))
    monkeypatch.setattr(gateway, "notify", lambda msg: calls.append(msg))

    gateway._toast("retry_loop", "stop re-running this")
    gateway._toast("retry_loop", "stop re-running this")   # debounced
    gateway._toast("budget_guardrail", "over budget")
    gateway._toast("collab", "")                            # empty line -> skipped
    assert calls == ["stop re-running this", "over budget"]
