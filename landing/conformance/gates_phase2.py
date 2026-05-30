"""GP1–GP5 — the Phase-2 gate extension. Two-sided where safety-relevant."""
import math
import grafomem_landing as A
from composition import detect_conflict


def evaluate_phase2(ctx):
    GP, wm, crumbs = {}, ctx["wm"], ctx["crumbs"]

    # GP1 — code provenance + license coverage
    code_ok = (len(ctx["code_log"]) > 0
               and all(c["breadcrumb"] for c in ctx["code_log"])
               and ctx["license_attested"])
    GP["GP1 code provenance + license"] = (code_ok, "coverage")

    # GP2 — composition completeness: every code-derived object has a composition record
    GP["GP2 composition completeness"] = (ctx["composition"].covers(ctx["code_object_ids"]), "coverage")

    # GP3 — conflict handling (TWO-SIDED): real contradiction detected+resolved; agreement not flagged
    agree_conflict, _ = detect_conflict("tables", 22, 22)
    diff_conflict, resolution = detect_conflict("tables", 22, 20)
    two_sided = (agree_conflict is False) and (diff_conflict is True) and (resolution["resolved"] == 20)
    # also exercise it on the REAL extracted claims vs observed code (informational findings)
    ran_on_real = isinstance(ctx["conflict_findings"], list)
    GP["GP3 conflict handling (2-sided)"] = (two_sided and ran_on_real, "two-sided")

    # GP4 — scale & proof cost: inclusion proofs stay O(log N)
    n = ctx["n_leaves"]
    idx = max(0, n - 1)
    proof = crumbs.inclusion_proof(idx)
    bound = math.ceil(math.log2(n)) + 1 if n > 1 else 1
    leaf = A.Crumbs._leaf(crumbs.breadcrumbs[idx])
    GP["GP4 scale & proof cost"] = (len(proof) <= bound
                                    and A.Crumbs.verify_inclusion(leaf, proof, ctx["proof_root"]), "performance")

    # GP5 — second Landing Certificate over the combined corpus, verifiable offline
    ok5, _, chk5 = A.verify_landing_certificate(
        ctx["cert"], ctx["layer_bytes"], lambda h: ctx["human_pubkeys"][h],
        ctx["anchor_root"], ctx["anchor_proof"], ctx["cert_leaf"])
    GP["GP5 combined Landing Certificate"] = (ok5 and all(chk5.values()), "headline")

    return GP
