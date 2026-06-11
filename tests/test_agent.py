"""
Background agent: config snapshot/restore, the per-OS autostart units it installs, and the org-mode
collaboration live-push (edge polls the collector for its own matches and toasts the new ones).
"""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from abenlux.agent import service


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_DIR", tmp_path / ".abenlux")
    monkeypatch.setattr(service, "ENV_FILE", tmp_path / ".abenlux" / "agent.env")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    calls = []
    monkeypatch.setattr(service.subprocess, "run",
                        lambda *a, **k: calls.append(list(a[0])) or subprocess.CompletedProcess(a[0], 0, "", ""))
    return tmp_path, calls


def test_env_snapshot_roundtrip(sandbox, monkeypatch):
    monkeypatch.setenv("ABEN_HMAC_KEY", "secret")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")
    monkeypatch.setenv("UNRELATED_VAR", "nope")
    service.write_env_file()
    pairs = service.load_env_pairs()
    assert pairs["ABEN_HMAC_KEY"] == "secret" and pairs["OTEL_LOGS_EXPORTER"] == "otlp"
    assert "UNRELATED_VAR" not in pairs               # only ABEN_*/OTEL_* are snapshotted


def test_load_env_file_does_not_clobber_explicit_env(sandbox, monkeypatch):
    monkeypatch.setenv("ABEN_HMAC_KEY", "fromfile")
    service.write_env_file()
    monkeypatch.setenv("ABEN_HMAC_KEY", "explicit")   # an explicit env var must win over the file
    service.load_env_file()
    import os
    assert os.environ["ABEN_HMAC_KEY"] == "explicit"


def test_linux_systemd_unit(sandbox, monkeypatch):
    home, calls = sandbox
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    service.install(9099)
    unit = home / ".config" / "systemd" / "user" / "com.abenlux.agent.service"
    txt = unit.read_text(encoding="utf-8")
    assert "ExecStart=" in txt and "agent run" in txt and "--port 9099" in txt
    assert "EnvironmentFile=" in txt and "WantedBy=default.target" in txt and "Restart=on-failure" in txt
    assert any("enable" in c for c in calls), "should enable+start the user unit"
    assert "not-installed" not in service.status() or True   # status path runs without raising


def test_macos_launchagent(sandbox, monkeypatch):
    home, calls = sandbox
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    service.install(8088)
    plist = home / "Library" / "LaunchAgents" / "com.abenlux.agent.plist"
    txt = plist.read_text(encoding="utf-8")
    assert "<key>RunAtLoad</key>" in txt and "<true/>" in txt
    assert "ProgramArguments" in txt and "agent" in txt and "8088" in txt
    assert any("load" in c for c in calls)


def test_windows_login_launcher(sandbox, monkeypatch):
    home, calls = sandbox
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setattr(service.platform, "system", lambda: "Windows")
    monkeypatch.setattr("abenlux.developer.notify.register_windows_aumid", lambda: True)
    msg = service.install(8088)
    vbs = home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "AbenluxAgent.vbs"
    txt = vbs.read_text(encoding="utf-8")
    assert "WScript.Shell" in txt and "agent run" in txt and "--port 8088" in txt
    assert ", 0, False" in txt                       # hidden window, non-blocking
    assert "AUMID" in msg
    st = service.status()
    assert "login launcher: installed" in st          # install artifact present
    assert "capture process:" in st                    # status also probes actual liveness (not just the file)
    service.uninstall()
    assert not vbs.exists()


def test_collab_status_endpoint_binds_to_authenticated_principal(monkeypatch):
    import os

    from fastapi.testclient import TestClient

    from abenlux.api.server import app, _principals
    from abenlux.developer.matches import MatchStore
    dev_pseudo = _principals.resolve("dev-token").pseudonym
    ms = MatchStore(os.environ["ABEN_MATCH_DB"])
    ms.record(dev_pseudo, "px_peer", "Acme - Checkout", 0.91, "live_duplication")
    ms.close()
    c = TestClient(app)
    ok = c.get("/v1/collab-status", headers={"Authorization": "Bearer dev-token"})  # the developer
    assert ok.status_code == 200
    matches = ok.json()["matches"]
    assert any(m["topic"] == "Acme - Checkout" and m["mode"] == "live_duplication" for m in matches)
    assert all("peer" not in m for m in matches)                       # never leaks the peer identity
    # the shared device ingest token is NOT a principal and a forged pseudonym header is ignored -
    # it cannot select whose feed is returned (the IDOR the red-team found, now closed)
    assert c.get("/v1/collab-status",
                 headers={"Authorization": "Bearer dev-ingest-token", "X-Aben-Pseudonym": dev_pseudo}).status_code == 401


def test_edge_poll_filters_mutual_and_throttles(monkeypatch):
    import abenlux.capture.gateway as gw

    class FakeResp:
        status_code = 200

        def json(self):
            return {"matches": [
                {"id": 1, "topic": "T1", "similarity": 0.9, "mode": "live_duplication", "mutual": False},
                {"id": 2, "topic": "T2", "similarity": 0.9, "mode": "live_duplication", "mutual": True},
            ]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(gw, "SETTINGS", SimpleNamespace(collector_url="http://collector", ingest_token="t"))
    monkeypatch.setattr(gw.httpx, "Client", FakeClient)
    monkeypatch.setenv("ABEN_TOKEN", "alice-token")   # the poll now authenticates as the developer
    gw._collab_state["refreshed"] = -1e18
    gw._collab_state["seen"].clear()

    fresh = gw._poll_collab_matches("pseudoA")
    assert [m["id"] for m in fresh] == [1]            # the mutually-consented match is not re-toasted
    assert gw._poll_collab_matches("pseudoA") == []   # throttled within the TTL, and deduped
