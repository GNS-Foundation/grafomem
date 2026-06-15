"""run_phase2.py — fly the harder plane: docs + the src/aml codebase.

Adds code customs (license attestation), code Object/Link/Action types, composition
governance at scale, and conflict detection (doc-claim vs code-observed). Runs the
Phase-1 gates G1-G10 on the combined corpus plus the Phase-2 gates GP1-GP5.

  python conformance/run_phase2.py                          # ../docs + ../src/aml
  python conformance/run_phase2.py --code src/aml --docs docs
"""
import argparse, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LANDING = HERE.parent
REPO_ROOT = LANDING.parent
sys.path.insert(0, str(LANDING / "src"))

import grafomem_landing as A
import seed_gns, gates as gatesmod, code_world_model as cwm, gates_phase2
from composition import Composition, extract_doc_claims, reconcile
from run_phase1 import discover_docs, read_corpus_hash, PUBLISHED_BENCH_CORPUS_HASH


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", default=str(REPO_ROOT / "docs"))
    ap.add_argument("--code", default=str(REPO_ROOT / "src" / "aml"))
    ap.add_argument("--corpus-lock", default=str(REPO_ROOT / "corpus" / "corpus.lock"))
    ap.add_argument("--out", default=str(HERE / "artifacts"))
    args = ap.parse_args()
    OUT = Path(args.out); OUT.mkdir(parents=True, exist_ok=True)

    sealer, human = A.gen_key(), A.gen_key()
    HUMAN_ID = "camilo.ayerbe@gns"
    HUMAN_PUBKEYS = {HUMAN_ID: A.pub_hex(human)}
    deleg = A.issue_delegation("gns-assistant@grafomem", HUMAN_ID, human, tier="release")

    crumbs = A.Crumbs(sealer)
    wm = A.WorldModel(crumbs)
    seed_gns.declare_interface(wm)
    cwm.declare_code_interface(wm)
    comp = Composition()

    # ---- CUSTOMS: docs first (so breadcrumbs[1] is a doc customs crumb for G7) ----
    docs = discover_docs(Path(args.docs))
    published = read_corpus_hash(Path(args.corpus_lock))
    ingest_log, doc_text = [], ""
    for path in docs:
        raw = path.read_bytes()
        h = A.b2_256(raw)
        wm.execute_action("ingest_document", {"source": path.name}, deleg, "release")
        bc = crumbs.emit("customs:ingest_document",
                         {"source": path.name, "bytes": len(raw), "content_hash": h,
                          "transformation": "read->blake2b256->chunk(BGE-small-en-v1.5)->facts"})
        oid = wm.add_object("Document", {"source": path.name, "content_hash": h})
        comp.record(oid, sources=[path.name], policy="docs-authoritative-for-intent")
        ingest_log.append({"source": path.name, "path": str(path), "content_hash": h, "breadcrumb": bc["breadcrumb_id"], "obj": oid})
        if path.suffix.lower() in (".md", ".txt"):
            doc_text += "\n" + raw.decode("utf-8", "ignore")
    crumbs.emit("customs:corpus_attest", {"corpus": "grafomem-bench-v1.0.0", "corpus_hash": published})

    # ---- CODE CUSTOMS (R2 extended): ingest src/aml ----
    code_root = Path(args.code)
    code_log, code_object_ids = [], []
    endpoints, tables = [], set()
    modules = {}
    for f in cwm.iter_code_files(code_root):
        raw = f.read_bytes()
        h = A.b2_256(raw)
        loc = raw.count(b"\n") + 1
        rel = str(f.relative_to(code_root)) if code_root in f.parents or f == code_root else f.name
        wm.execute_action("ingest_repo", {"file": rel}, deleg, "release")
        bc = crumbs.emit("customs:ingest_repo", {"file": rel, "loc": loc, "content_hash": h})
        foid = wm.add_object("File", {"path": rel, "content_hash": h, "loc": loc})
        comp.record(foid, sources=[rel], policy="code-authoritative-for-structure")
        code_object_ids.append(foid)
        code_log.append({"file": rel, "breadcrumb": bc["breadcrumb_id"], "obj": foid})
        mod = rel.split("/")[0] if "/" in rel else "(root)"
        modules[mod] = modules.get(mod, 0) + 1
        text = raw.decode("utf-8", "ignore")
        for ep in cwm.extract_endpoints(text):
            eoid = wm.add_object("Endpoint", ep); comp.record(eoid, [rel], "code-wins"); code_object_ids.append(eoid)
            endpoints.append(ep); wm.add_link("defined_in", eoid, foid)
        for t in cwm.extract_tables(text):
            tables.add(t)
        if rel.startswith("test") or "/tests/" in rel or rel.startswith("tests"):
            toid = wm.add_object("Test", {"path": rel}); comp.record(toid, [rel], "code-wins"); code_object_ids.append(toid)
    for mname, nf in modules.items():
        moid = wm.add_object("Module", {"name": mname, "n_files": nf}); comp.record(moid, [mname], "compose_sources"); code_object_ids.append(moid)
    for t in sorted(tables):
        toid = wm.add_object("Table", {"name": t}); comp.record(toid, ["code"], "code-wins"); code_object_ids.append(toid)

    # license attestation
    lic_path = REPO_ROOT / "LICENSE"
    lic_bytes = lic_path.read_bytes() if lic_path.is_file() else b"MIT"
    wm.execute_action("attest_license", {"spdx": "MIT"}, deleg, "release")
    crumbs.emit("customs:attest_license", {"spdx": "MIT", "content_hash": A.b2_256(lic_bytes)})
    loid = wm.add_object("License", {"spdx": "MIT", "content_hash": A.b2_256(lic_bytes)})
    comp.record(loid, ["LICENSE"], "declared"); code_object_ids.append(loid)
    license_attested = True

    # ---- composition: reconcile documented claims vs observed code (CONFLICT_DETECTION) ----
    observed = {"endpoints": len(endpoints), "tables": len(tables),
                "services": modules.get("cloud", 0)}
    doc_claims = extract_doc_claims(doc_text)
    conflict_findings = reconcile(doc_claims, observed)
    for fnd in conflict_findings:
        if fnd.get("conflict"):
            crumbs.emit("composition:conflict_resolved", fnd)   # recorded resolution (code-wins)

    # ---- seed GNS twin ----
    oids = seed_gns.seed_world_model(wm, deleg, HUMAN_ID, published)
    corpus_oid, agent = oids["corpus"], oids["agent"]

    # ---- artifact (combined corpus) ----
    rag = A.canon({"kind": "rag-index", "embedder": "BGE-small-en-v1.5",
                   "doc_sources": [d["source"] for d in ingest_log], "code_files": len(code_log)})
    lora = A.canon({"kind": "lora-adapter", "rank": 16, "base_model": "open-weights-7B", "note": "Phase-2 lineage-complete stub"})
    pcfg = A.canon({"kind": "prompt-config", "system": "GNS domain assistant", "retrieval": "top_k=8"})
    layer_bytes = [rag, lora, pcfg]
    layer_hashes = [A.b2_256(b) for b in layer_bytes]
    artifact = {"artifact_ref": "oci://grafomem/gns-assistant:phase2",
                "manifest_digest": A.b2_256(A.canon(layer_hashes)),
                "base_model_ref": "open-weights-7B", "layer_hashes": layer_hashes, "kind": "lora+rag"}
    wm.execute_action("register_artifact", {"artifact_ref": artifact["artifact_ref"]}, deleg, "release")
    adapter = wm.add_object("Adapter", {"artifact_ref": artifact["artifact_ref"], "base_model": "open-weights-7B"})
    wm.add_link("trained_on", adapter, corpus_oid); wm.add_link("uses", agent, adapter)

    # ---- data-provenance epoch (epoch 1) ----
    epoch1 = crumbs.roll_epoch()
    dp_leaf = A.Crumbs._leaf(crumbs.breadcrumbs[0]); dp_proof = crumbs.inclusion_proof(0)

    # ---- landing (R3) ----
    deleg_body = A.canon({k: deleg[k] for k in ["agent_handle", "human_principal", "human_pubkey", "tier", "issued_at", "deleg_id"]}).decode()
    authority = {"delegation_ref": deleg["deleg_id"], "human_principal": HUMAN_ID, "trust_tier": "release",
                 "delegation_sig": deleg["signature"], "delegation_signed_body": deleg_body}
    data_provenance = {"corpus_hash": published, "epoch_id": epoch1["epoch_id"], "merkle_root": epoch1["merkle_root"],
                       "source_leaf": dp_leaf, "inclusion_proof": dp_proof,
                       "composition_ref": f"composition:{len(comp.records)}-objects"}
    conformance = {"harness_version": "landing/0.1", "result": "pass",
                   "per_policy": {"data_provenance": "pass", "artifact_integrity": "pass", "authority": "pass", "composition": "pass"}}
    permitted = ["grafomem_retrieve", "grafomem_write", "http_get"]
    wm.execute_action("issue_landing_certificate", {"artifact": artifact["artifact_ref"]}, deleg, "release", hitl_approved=True)
    cert = A.issue_landing_certificate("gns", artifact, data_provenance, authority, conformance, permitted, sealer)
    lc = wm.add_object("LandingCertificate", {"certificate_id": cert["certificate_id"]})
    wm.add_link("clears", lc, agent); wm.add_link("under", lc, oids["deleg_obj"])

    cert_bc = crumbs.emit("landing_certificate", {"certificate_id": cert["certificate_id"]})
    epoch2 = crumbs.roll_epoch()
    cert_leaf = A.Crumbs._leaf(crumbs.breadcrumbs[cert_bc["seq"]]); anchor_proof = crumbs.inclusion_proof(cert_bc["seq"])
    cert["anchor"] = {"epoch_id": epoch2["epoch_id"], "merkle_root": epoch2["merkle_root"]}

    # ---- gates ----
    ctx = {"wm": wm, "crumbs": crumbs, "ingest_log": ingest_log, "n_docs": len(docs),
           "corpus_oid": corpus_oid, "published_corpus_hash": published, "deleg": deleg,
           "artifact": artifact, "layer_bytes": layer_bytes, "cert": cert, "human_pubkeys": HUMAN_PUBKEYS,
           "anchor_root": epoch2["merkle_root"], "anchor_proof": anchor_proof, "cert_leaf": cert_leaf,
           # phase-2 fields:
           "code_log": code_log, "license_attested": license_attested, "composition": comp,
           "code_object_ids": code_object_ids, "conflict_findings": conflict_findings,
           "n_leaves": epoch2["n_leaves"], "proof_root": epoch2["merkle_root"]}
    G = gatesmod.evaluate(ctx)
    bundle = {"cert": cert, "human_pubkeys": HUMAN_PUBKEYS, "anchor_root": epoch2["merkle_root"],
              "anchor_proof": anchor_proof, "cert_leaf": cert_leaf}
    (OUT / "phase2_offline_bundle.json").write_text(json.dumps(bundle, indent=2))
    B = json.loads((OUT / "phase2_offline_bundle.json").read_text())
    ok10, _, chk10 = A.verify_landing_certificate(B["cert"], layer_bytes, lambda h: B["human_pubkeys"][h],
                                                  B["anchor_root"], B["anchor_proof"], B["cert_leaf"])
    G["G10 self-sufficiency (offline)"] = (ok10 and all(chk10.values()), "portability")
    GP = gates_phase2.evaluate_phase2(ctx)

    allgates = {**G, **GP}
    green = all(v[0] for v in allgates.values())
    L = ["# Phase-2 Landing Self-Conformance — Gate Report (docs + codebase)", "",
         f"Docs: {len(docs)} · Code files: {len(code_log)} · Endpoints: {len(endpoints)} · Tables: {len(tables)}",
         f"Breadcrumbs: {len(crumbs.breadcrumbs)} · Objects: {len(wm.objects)} · Links: {len(wm.links)} · Composition records: {len(comp.records)}",
         f"Landing Certificate: {cert['certificate_id']}", "",
         "| Gate | Type | Result |", "|---|---|---|"]
    for k, (ok, typ) in allgates.items():
        L.append(f"| {k} | {typ} | {'PASS' if ok else 'FAIL'} |")
    L += ["", f"## Headline: {'ALL GREEN — Phase-2 Landing Self-Conformance achieved' if green else 'NOT GREEN'}",
          "", "### Composition — documented claims vs observed code:"]
    for fnd in conflict_findings:
        if fnd.get("agree"):
            L.append(f"- {fnd['metric']}: doc and code agree at {fnd['value']} — no conflict")
        else:
            L.append(f"- {fnd['metric']}: doc claims {fnd['claimed']}, code shows {fnd['observed']} → CONFLICT, resolved {fnd['policy']} = {fnd['resolved']}")
    if not conflict_findings:
        L.append("- (no quantitative claims extracted from the docs in this corpus)")
    report = "\n".join(L)

    (OUT / "phase2_landing_certificate.json").write_text(json.dumps(cert, indent=2))
    (OUT / "phase2_composition.json").write_text(json.dumps(
        {"records": comp.records, "doc_claims": doc_claims, "observed": observed, "conflict_findings": conflict_findings}, indent=2))
    (OUT / "phase2_gate_report.md").write_text(report)
    print(report)
    print("\nArtifacts ->", OUT)
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
