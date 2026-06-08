"""
Principal resolution: bearer token -> authenticated Principal. This is the scaffold's identity
layer, in deployment it is replaced by SSO/OIDC (the token becomes a verified JWT and the role
comes from the IdP / group membership). The shape stays identical, so the API doesn't change.

Tokens and their role mapping load from a YAML file (ABEN_PRINCIPALS) that lives with the rest of
the org config, version-controlled and access-controlled. Each principal's stored pseudonym is
derived with the SAME HMAC key the edge pipeline uses, so a person's authenticated identity maps
to the exact pseudonym their captured events were written under - which is what makes "show me my
own spend" resolve to the right rows without ever exposing the raw identity.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from abenlux.auth.rbac import Principal, Role
from abenlux.privacy.pseudonymize import pseudonymize
from abenlux.settings import SETTINGS


@dataclass
class PrincipalStore:
    _by_token: dict[str, Principal]

    @classmethod
    def from_yaml(cls, path: str, *, hmac_key: bytes) -> "PrincipalStore":
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        by_token: dict[str, Principal] = {}
        for p in data.get("principals", []):
            subject = p["subject"]
            contact = {k: p[k] for k in ("email", "slack", "teams", "github") if p.get(k)}
            by_token[p["token"]] = Principal(
                subject=subject,
                display_name=p.get("display_name", subject),
                role=Role(p.get("role", "developer")),
                pseudonym=pseudonymize(subject, hmac_key),
                contact=contact or None,
            )
        return cls(by_token)

    @classmethod
    def default_dev(cls, *, hmac_key: bytes) -> "PrincipalStore":
        """offline default so the API runs without config. obviously not for production."""
        people = [
            ("dev-token", "dev@example.com", "Dev Developer", Role.DEVELOPER, {"slack": "@dev", "email": "dev@example.com"}),
            ("mgr-token", "manager@example.com", "Morgan Manager", Role.MANAGER, None),
            ("fin-token", "finance@example.com", "Finn Finance", Role.FINANCE, {"slack": "@finn", "email": "finance@example.com"}),
            ("admin-token", "admin@example.com", "Avery Admin", Role.ADMIN, None),
        ]
        by_token = {
            tok: Principal(subj, name, role, pseudonymize(subj, hmac_key), contact=contact)
            for tok, subj, name, role, contact in people
        }
        return cls(by_token)

    def resolve(self, token: str | None) -> Principal | None:
        if not token:
            return None
        return self._by_token.get(token)

    def pseudonym_to_name(self, pseudonym: str) -> str | None:
        """reverse a pseudonym to a display name - used ONLY to reveal a peer's identity after a
        mutually-consented double-blind collaboration match. never exposed for analytics."""
        for p in self._by_token.values():
            if p.pseudonym == pseudonym:
                return p.display_name
        return None

    def pseudonym_to_contact(self, pseudonym: str) -> dict | None:
        """static contact card for a peer (name + handles), revealed ONLY on mutual consent."""
        for p in self._by_token.values():
            if p.pseudonym == pseudonym:
                card = {"name": p.display_name}
                card.update(p.contact or {})
                return card
        return None


def load_principals() -> PrincipalStore:
    path = os.getenv("ABEN_PRINCIPALS")
    if path and os.path.exists(path):
        return PrincipalStore.from_yaml(path, hmac_key=SETTINGS.hmac_bytes)
    return PrincipalStore.default_dev(hmac_key=SETTINGS.hmac_bytes)
