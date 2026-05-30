"""R4 — composition governance + conflict detection.

The composition record says which source(s) produced each world-model object, under
which selection policy. Conflict detection reconciles a documented claim ("22 tables")
against the observed code ("CREATE TABLE" count) and resolves by policy — the first real
workload for the reserved CONFLICT_DETECTION capability (backends/conflict_backends.py).
"""
import re

# selection policy: which source is authoritative for which metric
DEFAULT_POLICY = {"tables": "code-wins", "endpoints": "code-wins",
                  "services": "code-wins", "_default": "code-wins"}

_CLAIM = re.compile(r"(\d+)\s*\+?\s*(tables|endpoints|services)", re.I)


def extract_doc_claims(text):
    """Pull quantitative claims out of documentation text -> {metric: max_value_claimed}."""
    claims = {}
    for n, metric in _CLAIM.findall(text):
        m = metric.lower()
        claims[m] = max(claims.get(m, 0), int(n))
    return claims


def detect_conflict(metric, claimed, observed, policy=DEFAULT_POLICY):
    """Return (is_conflict, resolution). Resolution follows the selection policy."""
    if claimed == observed:
        return (False, {"metric": metric, "value": observed, "agree": True})
    winner = policy.get(metric, policy["_default"])
    resolved = observed if winner == "code-wins" else claimed
    return (True, {"metric": metric, "claimed": claimed, "observed": observed,
                   "policy": winner, "resolved": resolved})


def reconcile(doc_claims, observed):
    """Reconcile all doc claims against observed code counts -> list of findings."""
    findings = []
    for metric, claimed in doc_claims.items():
        obs = observed.get(metric, 0)
        is_conflict, detail = detect_conflict(metric, claimed, obs)
        detail["conflict"] = is_conflict
        findings.append(detail)
    return findings


class Composition:
    """Tracks which source(s) + policy produced each world-model object (R4)."""
    def __init__(self):
        self.records = {}   # obj_id -> {sources:[...], policy:str}

    def record(self, obj_id, sources, policy="declared"):
        self.records[obj_id] = {"sources": list(sources), "policy": policy}

    def covers(self, obj_ids):
        return all(oid in self.records for oid in obj_ids)
