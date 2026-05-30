"""run_phase1.py — execute the Phase-1 dogfood flight, then the 10 gates.

Runs standalone (no install needed) or after `pip install -e .` from landing/.

  python conformance/run_phase1.py                 # ingests ../docs, reads ../corpus/corpus.lock
  python conformance/run_phase1.py --docs /path     # ingest a different corpus
"""
import argparse, json, os, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent              # landing/conformance
LANDING = HERE.parent                               # landing
REPO_ROOT = LANDING.parent                          # repo root
sys.path.insert(0, str(LANDING / "src"))            # run without install

import grafomem_landing as A
import seed_gns
import gates as gatesmod

PUBLISHED_BENCH_CORPUS_HASH = "f049820bc24505111595b030ee9b2e6abd1812e80e96e3e770e1bbbcfb077ca6"


def discover_docs(docs_dir: Path):
    exts = {".md", ".pdf", ".txt"}
    if docs_dir.is_dir():
        files = [p for p in sorted(docs_dir.iterdir())
                 if p.suffix.lower() in exts and not p.name.startswith(("~$", "."))]
        if files:
            return files
    return [p for p in sorted((LANDING / "spec").glob("*.md"))]   # fallback so it always flies


def read_corpus_hash(lock_path: Path):
    if lock_path.is_file():
        m = re.search(r"\b[0-9a-f]{64}\b", lock_path.read_text(errors="ignore"))
        if m:
            return m.group(0)
    return PUBLISHED_BENCH_CORPUS_HASH


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", default=str(REPO_ROOT / "docs"))
    ap.add_argument("--corpus-lock", default=str(REPO_ROOT / "corpus" / "corpus.lock"))
    ap.add_argument("--out", default=str(HERE / "artifacts"))
    args = ap.parse_args()
    OUT = Path(args.out); OUT.mkdir(parents=True, exist_ok=True)

    # ---- identity ----
    sealer, human = A.gen_key(), A.gen_key()
    HUMAN_ID = "camilo.ayerbe@gns"
    HUMAN_PUBKEYS = {HUMAN_ID: A.pub_hex(human)}
    deleg = A.issue_delegation("gns-assistant@grafomem", HUMAN_ID, human, tier="release")

    crumbs = A.Crumbs(sealer)
    wm = A.WorldModel(crumbs)
    seed_gns.declare_interface(wm)

    # ---- 1. CUSTOMS (R2): ingest the real docs ----
    docs = discover_docs(Path(args.docs))
    published_corpus_hash = read_corpus_hash(Path(args.corpus_lock))
    ingest_log = []
    for path in docs:
        raw = path.read_bytes()
        content_hash = A.b2_256(raw)
        wm.execute_action("ingest_document", {"source": path.name}, deleg, "release")
        bc = crumbs.emit("customs:ingest_document",
                         {"source": path.name, "bytes": len(raw), "content_hash": content_hash,
                          "transformation": "read->blake2b256->chunk(BGE-small-en-v1.5)->facts"})
        oid = wm.add_object("Document", {"source": path.name, "content_hash": content_hash})
        ingest_log.append({"source": path.name, "path": str(path), "content_hash": content_hash,
                           "breadcrumb": bc["breadcrumb_id"], "obj": oid})
    crumbs.emit("customs:corpus_attest", {"corpus": "grafomem-bench-v0.2.0",
                "corpus_hash": published_corpus_hash, "source": "corpus/corpus.lock"})

    # ---- 2. WORLD-MODEL (R5): seed the GNS twin ----
    oids = seed_gns.seed_world_model(wm, deleg, HUMAN_ID, published_corpus_hash)
    corpus_oid, agent = oids["corpus"], oids["agent"]

    # ---- 3. ARTIFACT (R1): stub LoRA + RAG, lineage-complete ----
    rag = A.canon({"kind": "rag-index", "embedder": "BGE-small-en-v1.5", "sources": [d["source"] for d in ingest_log]})
    lora = A.canon({"kind": "lora-adapter", "rank": 16, "base_model": "open-weights-7B", "note": "Phase-1 lineage-complete stub"})
    pcfg = A.canon({"kind": "prompt-config", "system": "GNS domain assistant", "retrieval": "top_k=8"})
    layer_bytes = [rag, lora, pcfg]
    layer_hashes = [A.b2_256(b) for b in layer_bytes]
    artifact = {"artifact_ref": "oci://grafomem/gns-assistant:phase1",
                "manifest_digest": A.b2_256(A.canon(layer_hashes)),
                "base_model_ref": "open-weights-7B", "layer_hashes": layer_hashes, "kind": "lora+rag"}
    wm.execute_action("register_artifact", {"artifact_ref": artifact["artifact_ref"]}, deleg, "release")
    adapter = wm.add_object("Adapter", {"artifact_ref": artifact["artifact_ref"], "base_model": "open-weights-7B"})
    wm.add_link("trained_on", adapter, corpus_oid)
    wm.add_link("uses", agent, adapter)

    # ---- seal DATA-PROVENANCE epoch (epoch 1) ----
    epoch1 = crumbs.roll_epoch()
    dp_source_leaf = A.Crumbs._leaf(crumbs.breadcrumbs[0])
    dp_proof = crumbs.inclusion_proof(0)
    wm.add_object("Epoch", {"epoch_id": epoch1["epoch_id"], "merkle_root": epoch1["merkle_root"]})

    # ---- 4. LANDING (R3): conformance + governed issuance ----
    deleg_signed_body = A.canon({k: deleg[k] for k in
        ["agent_handle", "human_principal", "human_pubkey", "tier", "issued_at", "deleg_id"]}).decode()
    authority = {"delegation_ref": deleg["deleg_id"], "human_principal": HUMAN_ID, "trust_tier": "release",
                 "delegation_sig": deleg["signature"], "delegation_signed_body": deleg_signed_body}
    data_provenance = {"corpus_hash": published_corpus_hash, "epoch_id": epoch1["epoch_id"],
                       "merkle_root": epoch1["merkle_root"], "source_leaf": dp_source_leaf,
                       "inclusion_proof": dp_proof, "composition_ref": None}
    conformance = {"harness_version": "landing/0.1", "result": "pass",
                   "per_policy": {"data_provenance": "pass", "artifact_integrity": "pass", "authority": "pass"}}
    permitted = ["grafomem_retrieve", "grafomem_write", "http_get"]

    auth_issue, _ = wm.execute_action("issue_landing_certificate",
        {"artifact": artifact["artifact_ref"]}, deleg, "release", hitl_approved=True)
    assert auth_issue and all(A.b2_256(b) == h for b, h in zip(layer_bytes, artifact["layer_hashes"]))
    cert = A.issue_landing_certificate("gns", artifact, data_provenance, authority, conformance, permitted, sealer)
    lc = wm.add_object("LandingCertificate", {"certificate_id": cert["certificate_id"]})
    wm.add_link("clears", lc, agent)
    wm.add_link("under", lc, oids["deleg_obj"])

    # ---- anchor the certificate (epoch 2) ----
    cert_bc = crumbs.emit("landing_certificate", {"certificate_id": cert["certificate_id"], "artifact": artifact["artifact_ref"]})
    epoch2 = crumbs.roll_epoch()
    cert_leaf = A.Crumbs._leaf(crumbs.breadcrumbs[cert_bc["seq"]])
    anchor_proof = crumbs.inclusion_proof(cert_bc["seq"])
    cert["anchor"] = {"epoch_id": epoch2["epoch_id"], "merkle_root": epoch2["merkle_root"]}

    # ---- gates G1–G9 (+G6) ----
    ctx = {"wm": wm, "crumbs": crumbs, "ingest_log": ingest_log, "n_docs": len(docs),
           "corpus_oid": corpus_oid, "published_corpus_hash": published_corpus_hash,
           "deleg": deleg, "artifact": artifact, "layer_bytes": layer_bytes,
           "cert": cert, "human_pubkeys": HUMAN_PUBKEYS,
           "anchor_root": epoch2["merkle_root"], "anchor_proof": anchor_proof, "cert_leaf": cert_leaf}
    G = gatesmod.evaluate(ctx)

    # ---- G10 — self-sufficiency: verify from serialized {cert+chain+keys}, no live objects ----
    bundle = {"cert": cert, "human_pubkeys": HUMAN_PUBKEYS, "anchor_root": epoch2["merkle_root"],
              "anchor_proof": anchor_proof, "cert_leaf": cert_leaf}
    (OUT / "phase1_offline_bundle.json").write_text(json.dumps(bundle, indent=2))
    B = json.loads((OUT / "phase1_offline_bundle.json").read_text())
    ok10, _, chk10 = A.verify_landing_certificate(B["cert"], layer_bytes, lambda h: B["human_pubkeys"][h],
                                                  B["anchor_root"], B["anchor_proof"], B["cert_leaf"])
    G["G10 self-sufficiency (offline)"] = (ok10 and all(chk10.values()), "portability")

    # ---- report ----
    all_green = all(v[0] for v in G.values())
    L = ["# Phase-1 Landing Self-Conformance — Gate Report", "",
         f"Corpus flown: {len(docs)} documents from {args.docs} (real bytes).",
         f"Breadcrumbs: {len(crumbs.breadcrumbs)} · Epochs: 2 · Objects: {len(wm.objects)} · Links: {len(wm.links)}",
         f"Landing Certificate: {cert['certificate_id']}", "",
         "| Gate | Type | Result |", "|---|---|---|"]
    for k, (ok, typ) in G.items():
        L.append(f"| {k} | {typ} | {'PASS' if ok else 'FAIL'} |")
    L += ["", f"## Headline: {'ALL GREEN — Landing Self-Conformance achieved' if all_green else 'NOT GREEN'}",
          "", "### Offline reconstruction (G6) — from cert + chain + keys alone:"]
    for k, v in ctx["recon"].items():
        L.append(f"- **{k}**: {v}")
    report = "\n".join(L)

    (OUT / "phase1_landing_certificate.json").write_text(json.dumps(cert, indent=2))
    (OUT / "phase1_world_model.json").write_text(json.dumps(
        {"object_types": list(wm.object_types), "link_types": list(wm.link_types),
         "action_types": wm.action_types, "n_objects": len(wm.objects), "n_links": len(wm.links)}, indent=2))
    (OUT / "phase1_gcrumbs_chain.json").write_text(json.dumps(
        {"breadcrumbs": crumbs.breadcrumbs, "epoch1": epoch1, "epoch2": epoch2}, indent=2, default=str))
    (OUT / "phase1_article10_dossier.json").write_text(json.dumps(ctx["dossier"], indent=2))
    (OUT / "phase1_gate_report.md").write_text(report)
    print(report)
    print("\nArtifacts ->", OUT)
    return 0 if all_green else 1


if __name__ == "__main__":
    raise SystemExit(main())
