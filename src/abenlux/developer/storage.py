"""
On-device storage helpers for the developer-private stores (matches, contacts). The STORAGE LOCATION
is part of the privacy guarantee: a developer's match peers and contact card must live in their own
home directory with restrictive permissions, not in a world-readable, shared working directory. A
shared collector deployment overrides the path explicitly (ABEN_MATCH_DB / ABEN_CONTACT_DB).
"""
from __future__ import annotations

import os
from pathlib import Path


def private_dir() -> Path:
    d = Path.home() / ".abenlux"
    try:
        d.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(d, 0o700)              # owner-only
    except Exception:
        pass                               # best effort - never block on a permissions quirk
    return d


def private_db_path(name: str) -> str:
    return str(private_dir() / name)


def secure_file(path: str) -> None:
    # restrict the sqlite file to the owner so a co-tenant on a shared host can't read it off disk.
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
