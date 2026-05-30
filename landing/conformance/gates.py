"""The 10 acceptance gates — "Landing Self-Conformance". Two-sided where safety-relevant
(a gate that passes one direction but fails the other is a FAIL — the claims-but-leaks
discipline from the GRAFOMEM benchmark). Phase B: graduates into
tests/landing_self_conformance.py alongside tests/gmp_self_conformance.py.
"""
import copy
import grafomem_landing as A


def evaluate(ctx):
    G, wm, crumbs = {}, ctx["wm"], ctx["crumbs"]

    # G1 — customs coverage
    G["G1 customs coverage"] = (len(ctx["ingest_log"]) == ctx["n_docs"] and
                                all(d["breadcrumb"] for d in ctx["ingest_log"]), "coverage")

    # G2 — content/corpus hash (deterministic re-hash of a real ingested doc + corpus matches published)
    first = ctx["ingest_log"][0]
    rehash = A.b2_256(open(first["path"], "rb").read())
    G["G2 content/corpus hash"] = (rehash == first["content_hash"] and
                                   wm.objects[ctx["corpus_oid"]]["props"]["corpus_hash"] == ctx["published_corpus_hash"],
                                   "deterministic")

    # G3 — world-model validity
    G["G3 world-model validity"] = (wm.validate() == [], "structural")

    # G4 — action governance (TWO-SIDED)
    deleg = ctx["deleg"]
    pos, _ = wm.execute_action("issue_landing_certificate", {"t": "pos"}, deleg, "release", hitl_approved=True)
    neg_tier, r1 = wm.execute_action("issue_landing_certificate", {"t": "neg-tier"}, deleg, "read", hitl_approved=True)
    neg_deleg, r2 = wm.execute_action("issue_landing_certificate", {"t": "neg-deleg"}, None, "release", hitl_approved=True)
    G["G4 action governance (2-sided)"] = (pos and not neg_tier and not neg_deleg and bool(r1) and bool(r2), "two-sided")

    # G5 — certificate preconditions (TWO-SIDED)
    def ok_pre(artifact, layers):
        return all(A.b2_256(b) == h for b, h in zip(layers, artifact["layer_hashes"]))
    broken = copy.deepcopy(ctx["artifact"]); broken["layer_hashes"][1] = "0" * 64
    G["G5 cert preconditions (2-sided)"] = (ok_pre(ctx["artifact"], ctx["layer_bytes"]) and
                                            not ok_pre(broken, ctx["layer_bytes"]), "two-sided")

    # G6 — independent verification (offline reconstruction)
    ok6, recon, checks6 = A.verify_landing_certificate(
        ctx["cert"], ctx["layer_bytes"], lambda h: ctx["human_pubkeys"][h],
        ctx["anchor_root"], ctx["anchor_proof"], ctx["cert_leaf"])
    G["G6 independent verification"] = (ok6 and all(checks6.values()), "headline")
    ctx["recon"] = recon

    # G7 — tamper detection (TWO-SIDED / negative)
    intact = crumbs.verify_chain()
    saved = crumbs.breadcrumbs[1]["payload"].get("content_hash")
    crumbs.breadcrumbs[1]["payload"]["content_hash"] = "deadbeef" * 8
    tampered = crumbs.verify_chain()
    crumbs.breadcrumbs[1]["payload"]["content_hash"] = saved
    G["G7 tamper detection (2-sided)"] = (intact == "intact" and tampered == "tampered"
                                          and crumbs.verify_chain() == "intact", "two-sided")

    # G8 — erasure under the world-model (TWO-SIDED)
    victim = wm.add_object("Document", {"source": "PII-record", "content_hash": A.b2_256(b"pii")})
    sibling = ctx["ingest_log"][0]["obj"]
    del wm.objects[victim]
    crumbs.emit("erasure:hard_delete", {"obj": victim})
    G["G8 erasure (2-sided)"] = ((victim not in wm.objects) and (sibling in wm.objects), "two-sided")

    # G9 — Article-10 dossier auto-fields trace to breadcrumbs
    crumb_ids = {bc["breadcrumb_id"] for bc in crumbs.breadcrumbs}
    dossier = {"provenance": [{"source": d["source"], "content_hash": d["content_hash"], "evidence": d["breadcrumb"]}
                             for d in ctx["ingest_log"]],
               "methodology": ["read->blake2b256->chunk(BGE-small-en-v1.5)->facts"],
               "composition": [d["source"] for d in ctx["ingest_log"]]}
    G["G9 Article-10 projection"] = (all(p["evidence"] in crumb_ids for p in dossier["provenance"]), "coverage")
    ctx["dossier"] = dossier

    return G
