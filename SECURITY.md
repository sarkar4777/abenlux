# Security Policy

Abenlux processes developer prompts in-flight on the device. Its security posture is the product,
so we take reports seriously.

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security -> Advisories) or email the maintainer
rather than filing a public issue. Include a description, reproduction, and impact. We aim to
acknowledge within a few days.

## Threat model and invariants

The design assumes a curious-but-not-malicious analytics plane and a works-council/GDPR setting.
The invariants a report should test against:

- **Edge redaction precedes persistence.** Secrets/PII are destroyed before any derive or write.
  A finding that gets raw content or a credential into the derived store or onto the collector is
  high severity.
- **Derived-only forwarding.** The collector's only write path (`/v1/derived`) accepts content-free
  records and drops unknown fields. Smuggling content through it is a finding.
- **One-way identity.** Actors are HMAC-pseudonymized at the edge with a key the analytics plane
  cannot read. De-anonymization from inside analytics is a finding.
- **No individual management view.** No RBAC permission exposes another person's rows. A path that
  does is a finding.

## Operational hardening (deployers)

- Put the collector behind TLS and SSO. Replace the static principals file with your IdP.
- Keep `ABEN_HMAC_KEY` in a secret store the analytics plane cannot read. Rotate per policy.
- Use per-device ingest tokens (`ABEN_INGEST_TOKENS`) and rotate them.
- The gateway binds loopback by default. Do not expose it off-device.
