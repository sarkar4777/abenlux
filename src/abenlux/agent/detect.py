"""
Tool detection for the desktop agent. Which AI tool produced a call is the join key for the
whole tool-mix report, but a token event by itself doesn't say. We resolve it cross-platform
(Windows / macOS / Linux) with three signals, most to least reliable:

  1. explicit override   - ABEN_TOOL, set by an operator or a tool-specific launcher.
  2. environment markers - most CLIs/IDEs export a tell-tale var into the child shell
                           (Claude Code sets CLAUDECODE=1, Cursor sets CURSOR_TRACE_ID, etc.).
                           This is the sweet spot: zero deps, deterministic, OS-independent.
  3. process ancestry    - walk parent processes and match the image name against known tools.
                           Uses psutil when present (robust on all three OSes), otherwise a
                           best-effort single-level parent probe via the platform's own CLI.

Everything is best-effort and never raises: an unknown tool is a first-class outcome (returns
None), not an error. Detection reads only process/-env metadata - never window titles, never
keystrokes, never screen content - consistent with the content-free work-context rule.
"""
from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Optional

# env var -> (tool, app_category). presence of the var is the signal.
_ENV_MARKERS: list[tuple[str, str, str]] = [
    ("CLAUDECODE", "claude-code", "cli"),
    ("CLAUDE_CODE_ENTRYPOINT", "claude-code", "cli"),
    ("CURSOR_TRACE_ID", "cursor-agent", "ide"),
    ("CODEX_SANDBOX", "openai-codex", "cli"),
    ("CODEX_HOME", "openai-codex", "cli"),
    ("AIDER_MODEL", "aider", "cli"),
    ("GEMINI_CLI", "gemini-cli", "cli"),
    ("CLINE_API_KEY", "cline", "ide"),
    ("CONTINUE_GLOBAL_DIR", "continue", "ide"),
]

# substring of a process image name -> (tool, app_category). lowercased compare.
_PROC_MARKERS: list[tuple[str, str, str]] = [
    ("claude", "claude-code", "cli"),
    ("codex", "openai-codex", "cli"),
    ("cursor", "cursor-agent", "ide"),
    ("aider", "aider", "cli"),
    ("gemini", "gemini-cli", "cli"),
    ("opencode", "opencode", "cli"),
    ("crush", "crush", "cli"),
    ("forge", "forgecode", "cli"),
    ("droid", "droid", "cli"),
    ("windsurf", "windsurf", "ide"),
    ("zed", "zed", "ide"),
    ("\bpi\b", "pi", "cli"),
]


@dataclass
class Detection:
    tool: Optional[str]
    app_category: str
    source: str  # "override" | "env" | "process" | "unknown"


def _from_env() -> Optional[Detection]:
    if os.getenv("ABEN_TOOL"):
        return Detection(os.environ["ABEN_TOOL"], os.getenv("ABEN_APP_CATEGORY", "cli"), "override")
    # TERM_PROGRAM distinguishes the Cursor/VS Code terminal family
    term = (os.getenv("TERM_PROGRAM") or "").lower()
    if "cursor" in term:
        return Detection("cursor-agent", "ide", "env")
    for var, tool, cat in _ENV_MARKERS:
        if os.getenv(var):
            return Detection(tool, cat, "env")
    return None


def _ancestor_names(max_depth: int = 8) -> list[str]:
    """parent-process image names, nearest first. psutil if available, else best-effort."""
    try:
        import psutil  # optional, robust cross-platform ancestry
        names: list[str] = []
        proc = psutil.Process(os.getpid()).parent()
        while proc is not None and len(names) < max_depth:
            try:
                names.append(proc.name())
                proc = proc.parent()
            except psutil.Error:
                break
        return names
    except Exception:
        return _immediate_parent_name()


def _immediate_parent_name() -> list[str]:
    """single-level parent image name without psutil. tolerant of any failure."""
    ppid = os.getppid()
    sysname = platform.system()
    try:
        if sysname == "Windows":
            out = subprocess.run(
                ["tasklist", "/fi", f"PID eq {ppid}", "/nh", "/fo", "csv"],
                capture_output=True, text=True, timeout=2.0,
            ).stdout
            # first CSV field is "Image Name"
            if '"' in out:
                return [out.split('"')[1]]
        elif sysname == "Linux" and os.path.exists(f"/proc/{ppid}/comm"):
            with open(f"/proc/{ppid}/comm", encoding="utf-8") as fh:
                return [fh.read().strip()]
        else:  # macOS / other unix
            out = subprocess.run(
                ["ps", "-p", str(ppid), "-o", "comm="],
                capture_output=True, text=True, timeout=2.0,
            ).stdout.strip()
            if out:
                return [os.path.basename(out)]
    except Exception:
        pass
    return []


def _from_process() -> Optional[Detection]:
    for name in _ancestor_names():
        low = name.lower()
        for needle, tool, cat in _PROC_MARKERS:
            token = needle.strip("\\b")
            if token == "pi":
                # avoid matching 'pip', 'python', require exact stem
                stem = os.path.splitext(low)[0]
                if stem == "pi":
                    return Detection(tool, cat, "process")
                continue
            if token in low:
                return Detection(tool, cat, "process")
    return None


def detect() -> Detection:
    """resolve the active tool. order: override -> env -> process ancestry -> unknown."""
    return _from_env() or _from_process() or Detection(None, os.getenv("ABEN_APP_CATEGORY", "cli"), "unknown")


def detect_tool() -> Optional[str]:
    return detect().tool
