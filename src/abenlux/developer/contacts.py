"""
Contact cards for collaboration. A double-blind match is only useful if the two developers can
actually reach each other once they both opt in. So each developer registers a small contact card -
the handles they are willing to share (Slack, Teams, email, GitHub) - and the card is revealed to a
peer ONLY after mutual consent. Until then it stays hidden, exactly like the identity.

The developer controls their own card and what's in it. It lives keyed by pseudonym, so the store
never needs the raw identity, and there is no management read path to it.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from abenlux.developer.storage import private_db_path, secure_file

# self-set handle fields only. 'name' is deliberately NOT here: the revealed identity is the
# IdP-verified display name, not a self-chosen string, so a peer can't present an attacker-picked name.
FIELDS = ("email", "slack", "teams", "github", "note")

_SCHEMA = "CREATE TABLE IF NOT EXISTS contacts (pseudonym TEXT PRIMARY KEY, card TEXT)"


def clean_card(data: dict) -> dict:
    # keep only known handle fields, drop blanks
    return {k: str(v).strip() for k, v in data.items() if k in FIELDS and str(v).strip()}


class ContactStore:
    def __init__(self, path: str | Path | None = None):
        path = str(path) if path is not None else private_db_path("contacts.db")
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(_SCHEMA)
        self.conn.commit()
        secure_file(path)

    def set(self, pseudonym: str, card: dict) -> dict:
        card = clean_card(card)
        self.conn.execute("INSERT OR REPLACE INTO contacts (pseudonym, card) VALUES (?,?)",
                          (pseudonym, json.dumps(card)))
        self.conn.commit()
        return card

    def get(self, pseudonym: str) -> dict | None:
        row = self.conn.execute("SELECT card FROM contacts WHERE pseudonym=?", (pseudonym,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        self.conn.close()
