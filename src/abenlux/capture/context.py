"""
Work-context capture. The coarse, CONTENT-FREE signals captured at the moment of a call:
active tool, repo, git branch (which encodes the ticket id), workspace, OS. This is the
attribution join key - never the prompt body, never keystrokes, never screen content.

Sources in deployment: the desktop agent reads foreground-app metadata, and a lightweight
git probe in the active workspace. Here we read git + env so the scaffold works immediately.
"""
from __future__ import annotations

import os
import platform
import subprocess

from abenlux.agent.detect import detect
from abenlux.attribution.attributor import extract_ticket
from abenlux.schema import WorkContext


def _git(args: list[str], cwd: str | None) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=1.5
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def current_work_context(cwd: str | None = None) -> WorkContext:
    cwd = cwd or os.getcwd()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    remote = _git(["config", "--get", "remote.origin.url"], cwd)
    repo = None
    if remote:
        repo = remote.rstrip("/").split("/")[-1].removesuffix(".git")
    det = detect()  # override -> env markers -> process ancestry
    return WorkContext(
        tool=det.tool,
        app_category=det.app_category,
        repo=repo,
        git_branch=branch,
        ticket_id=extract_ticket(branch),
        workspace=os.path.basename(cwd),
        host_os=platform.system(),
    )


def current_actor() -> str:
    # raw id ONLY in-flight, pseudonymized before persistence
    return os.getenv("ABEN_ACTOR") or os.getenv("USER") or os.getenv("USERNAME") or "anon"
