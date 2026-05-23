"""
GRAFOMEM structured compliance report — machine-readable conformance output.

Converts a ConformanceProfile (from eval/conformance.py) and optional workload
run scores into a ComplianceReport dataclass, serializable to JSON and Markdown.

This is the artifact a customer shows their compliance team:

    grafomem conformance -b my.module:MyBackend -o report.json

The JSON schema is stable across minor versions; fields are additive-only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class DirectionVerdict:
    """One direction of a capability test (e.g. 'leakage ≤ eps')."""
    name: str
    objective: str
    point: float
    ci: tuple[float, float]
    passed: bool


@dataclass
class CapabilityVerdict:
    """Conformance verdict for one declared capability."""
    capability: str
    workload: str
    passed: bool
    directions: list[DirectionVerdict]


@dataclass
class SafetyChecks:
    """Binary safety checks (Check L and Check P)."""
    check_l: str  # "pass" | "fail" | "n/a"
    check_p: str  # "pass" | "fail" | "n/a"


@dataclass
class WorkloadScore:
    """Per-workload M1–M4 scores."""
    workload: str
    m1: float | None = None
    m2: float | None = None
    m3: float | None = None
    m4: dict[str, Any] | None = None  # op -> {p50, p95, p99}


@dataclass
class ComplianceReport:
    """The top-level compliance artifact.

    JSON-serializable; fields are additive-only across minor versions.
    """
    # Identity
    store_name: str
    grafomem_version: str
    corpus_hash: str
    timestamp: str

    # Conformance
    m8_conformance_rate: float
    declared_capabilities: list[str]
    supported_capabilities: list[str]
    capability_verdicts: list[CapabilityVerdict]

    # Optional: workload scores (populated by `grafomem run`)
    workload_scores: list[WorkloadScore] = field(default_factory=list)

    # Safety
    safety_checks: SafetyChecks = field(
        default_factory=lambda: SafetyChecks(check_l="n/a", check_p="n/a")
    )

    # Violations summary
    violations: list[str] = field(default_factory=list)

    # Cryptographic signature (optional — set by sign_report())
    signature: str | None = None       # hex-encoded Ed25519 signature
    signed_by: str | None = None       # hex-encoded Ed25519 public key


def from_profile(profile, *, corpus_hash: str = "unknown") -> ComplianceReport:
    """Build a ComplianceReport from a ConformanceProfile."""
    import importlib.metadata
    try:
        version = importlib.metadata.version("grafomem")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    verdicts = []
    for r in profile.results:
        verdicts.append(CapabilityVerdict(
            capability=r.capability.value,
            workload=r.workload,
            passed=r.passed,
            directions=[
                DirectionVerdict(
                    name=d.name,
                    objective=d.objective,
                    point=d.point,
                    ci=(d.ci[0], d.ci[1]) if d.ci else (0.0, 0.0),
                    passed=d.passed,
                )
                for d in r.directions
            ],
        ))

    # Safety checks from profile
    check_l = "n/a"
    check_p = "n/a"
    for r in profile.results:
        cap = r.capability.value
        if cap == "hard_delete":
            check_l = "pass" if r.passed else "fail"
        if cap == "cryptographic_provenance":
            check_p = "pass" if r.passed else "fail"

    return ComplianceReport(
        store_name=profile.store,
        grafomem_version=version,
        corpus_hash=corpus_hash,
        timestamp=datetime.now(timezone.utc).isoformat(),
        m8_conformance_rate=profile.conformance_rate,
        declared_capabilities=sorted(c.value for c in profile.declared),
        supported_capabilities=sorted(c.value for c in profile.supported),
        capability_verdicts=verdicts,
        safety_checks=SafetyChecks(check_l=check_l, check_p=check_p),
        violations=[r.capability.value for r in profile.violations],
    )


def _canonical_payload(report: ComplianceReport) -> str:
    """Canonical JSON for signing: sorted keys, compact separators,
    signature/signed_by excluded (they aren't part of the signed content)."""
    d = asdict(report)
    d.pop("signature", None)
    d.pop("signed_by", None)
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)


def to_json(report: ComplianceReport) -> str:
    """Serialize to JSON string."""
    return json.dumps(asdict(report), indent=2, default=str)


def sign_report(report: ComplianceReport, signing_key: bytes) -> ComplianceReport:
    """Ed25519-sign a ComplianceReport. Returns a new report with signature
    and signed_by fields populated.

    signing_key: 32-byte Ed25519 private seed (same format as WriteOptions.signing_key).
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat,
        )
    except ImportError as exc:
        raise RuntimeError(
            "sign_report requires grafomem[crypto] (pip install grafomem[crypto])"
        ) from exc

    payload = _canonical_payload(report).encode("utf-8")
    priv = Ed25519PrivateKey.from_private_bytes(signing_key)
    sig = priv.sign(payload)
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    # Return a new report with signature fields set
    from dataclasses import replace
    return replace(report, signature=sig.hex(), signed_by=pub.hex())


def verify_report(report: ComplianceReport) -> bool:
    """Verify the Ed25519 signature on a ComplianceReport.
    Returns True if the signature is valid, False otherwise.
    Returns False if the report is unsigned."""
    if not report.signature or not report.signed_by:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False

    payload = _canonical_payload(report).encode("utf-8")
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(report.signed_by))
        pub.verify(bytes.fromhex(report.signature), payload)
        return True
    except Exception:
        return False


def to_markdown(report: ComplianceReport) -> str:
    """Render a human-readable Markdown compliance report."""
    lines = [
        f"# GRAFOMEM Compliance Report",
        f"",
        f"| Field | Value |",
        f"|---|---|",
        f"| **Store** | {report.store_name} |",
        f"| **GRAFOMEM version** | {report.grafomem_version} |",
        f"| **Corpus hash** | `{report.corpus_hash[:16]}...` |",
        f"| **Timestamp** | {report.timestamp} |",
        f"| **M8 Conformance Rate** | **{report.m8_conformance_rate:.3f}** |",
        f"",
        f"---",
        f"",
        f"## Declared Capabilities",
        f"",
        f"{', '.join(f'`{c}`' for c in report.declared_capabilities)}",
        f"",
        f"## Conformance Verdicts",
        f"",
        f"| Capability | Workload | Verdict |",
        f"|---|---|---|",
    ]
    for v in report.capability_verdicts:
        icon = "✅" if v.passed else "❌"
        lines.append(f"| `{v.capability}` | {v.workload} | {icon} {'PASS' if v.passed else 'FAIL'} |")

    lines.extend([
        f"",
        f"## Safety Checks",
        f"",
        f"| Check | Status |",
        f"|---|---|",
        f"| Check L (deletion leakage) | {report.safety_checks.check_l.upper()} |",
        f"| Check P (provenance) | {report.safety_checks.check_p.upper()} |",
    ])

    if report.violations:
        lines.extend([
            f"",
            f"## ⚠️ Violations",
            f"",
        ])
        for v in report.violations:
            lines.append(f"- `{v}`")

    if report.m8_conformance_rate == 1.0 and not report.violations:
        lines.extend([
            f"",
            f"---",
            f"",
            f"> **Result:** All declared capabilities pass the GMP conformance suite. "
            f"M8 = 1.000. The store is conformant.",
        ])
    else:
        lines.extend([
            f"",
            f"---",
            f"",
            f"> **Result:** M8 = {report.m8_conformance_rate:.3f}. "
            f"{len(report.violations)} violation(s) detected. "
            f"The store does NOT fully conform to its declared capabilities.",
        ])

    # Signature status
    if report.signature:
        lines.extend([
            f"",
            f"## 🔐 Cryptographic Signature",
            f"",
            f"| Field | Value |",
            f"|---|---|",
            f"| Signed by | `{report.signed_by[:16]}...` |",
            f"| Signature | `{report.signature[:32]}...` |",
            f"| Algorithm | Ed25519 |",
            f"| Status | {'✅ Valid' if verify_report(report) else '❌ Invalid'} |",
        ])

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from aml.backends.gmp_reference import GMPReferenceBackend
    from aml.backends.vector_only import _stub_embedder
    from aml.eval.conformance import run_conformance

    profile = run_conformance(
        lambda: GMPReferenceBackend(embed_fn=_stub_embedder()),
        name="GMPReferenceBackend",
        seeds=range(1),
    )
    report = from_profile(profile, corpus_hash="f049820bc24505111595b030ee9b2e6abd1812e80e96e3e770e1bbbcfb077ca6")

    print("=== JSON ===")
    print(to_json(report)[:500], "...\n")

    print("=== MARKDOWN ===")
    print(to_markdown(report))

    print("✓ Report module smoke green.")
