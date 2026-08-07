"""Deterministic per-tenant-keyed pseudonymization of `invoice_ref` (last PII residual).

`invoice_ref` (= `invoice_id`) is `OUT-{company}-{person}` — real prospect PII — but it must
stay QUERYABLE (CGR joins on it, plaintext in `decision_records.parameters` + the CGR outcome/
review `subject`). So we replace it with a pseudonym that preserves the join and leaks nothing:

    pseudo = "OUT-" + HMAC_SHA256(k_tenant, invoice_ref)[:24 hex]
    k_tenant = HMAC_SHA256(master_key, DOMAIN + tenant_id)

* deterministic  → same (tenant, ref) always maps to the same pseudo, so the pure-equality
  join (decision.parameters.invoice_ref == outcome.metadata.subject) is preserved as long as
  BOTH sides are pseudonymized with the same tenant key.
* per-tenant + domain-separated key  → no cross-tenant correlation; the HMAC key is derived
  from GRAFOMEM_MASTER_KEY, distinct from the encryption DEKs (key-separation by purpose).
* irreversible, no mapping table, no plaintext stored  → the original name is unrecoverable.

The "OUT-" prefix is COSMETIC (Step-0 confirmed no code parses/splits the ref). Idempotency: a
pseudonym matches `^OUT-[0-9a-f]{24}$`; raw refs never do — so re-running the backfill is a no-op.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re

logger = logging.getLogger("grafomem.invoice_pseudonym")

_DOMAIN = b"grafomem.invoice-ref-pseudo.v1:"
_PREFIX = "OUT-"
_N = 24                                   # hex chars of digest (96 bits — no collision risk at this scale)
_PSEUDO_RE = re.compile(r"^OUT-[0-9a-f]{24}$")


def is_pseudonymized(invoice_ref: str | None) -> bool:
    """True if the value is already an OUT-<24hex> pseudonym (idempotency guard)."""
    return bool(invoice_ref) and bool(_PSEUDO_RE.match(invoice_ref))


def _master_key_hex() -> str | None:
    return os.environ.get("GRAFOMEM_MASTER_KEY")


def _tenant_key(tenant_id: str, master_key_hex: str) -> bytes:
    # domain-separated per-tenant HMAC key derived from the master (deterministic, unstored)
    return hmac.new(bytes.fromhex(master_key_hex)[:32], _DOMAIN + tenant_id.encode(), hashlib.sha256).digest()


def pseudonymize(invoice_ref: str | None, tenant_id: str, *, master_key_hex: str | None = None) -> str | None:
    """Map a raw invoice_ref to its per-tenant pseudonym. None → None. Already-pseudonymized
    input is returned unchanged (idempotent). Reads GRAFOMEM_MASTER_KEY unless a key is passed."""
    if invoice_ref is None:
        return None
    if is_pseudonymized(invoice_ref):
        return invoice_ref
    mk = master_key_hex or _master_key_hex()
    if not mk:
        # FAIL-OPEN only where the master key is genuinely absent (test/dev): the app cannot
        # boot in prod without GRAFOMEM_MASTER_KEY (app.py fail-closed), so prod ALWAYS
        # pseudonymizes; this branch never runs there. Warn loudly so a misconfig is visible.
        logger.warning("GRAFOMEM_MASTER_KEY absent — invoice_ref left RAW (no pseudonymization). "
                       "Expected only in test/dev; NEVER in prod.")
        return invoice_ref
    digest = hmac.new(_tenant_key(tenant_id, mk), invoice_ref.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{_PREFIX}{digest[:_N]}"
