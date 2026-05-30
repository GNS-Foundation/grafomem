"""The GNS starter world-model — GNS modelling itself (the digital twin, recursive).
Declares the R5 interface (Object/Link/Action types) and seeds the static GNS objects.
"""

OBJECT_TYPES = [
    ("Protocol", ["name", "version", "status"], ["name", "version"]),
    ("Capability", ["name", "state"], ["name"]),
    ("Backend", ["name", "declared_capabilities"], ["name"]),
    ("Corpus", ["corpus_hash", "n_traces"], ["corpus_hash"]),
    ("ConformanceReport", ["subject_backend", "m8_score"], ["subject_backend", "m8_score"]),
    ("Agent", ["handle", "role", "model_id"], ["handle"]),
    ("DelegationCertificate", ["deleg_id", "human_principal", "tier"], ["deleg_id"]),
    ("Adapter", ["artifact_ref", "base_model"], ["artifact_ref"]),
    ("Document", ["source", "content_hash"], ["source"]),
    ("Epoch", ["epoch_id", "merkle_root"], ["epoch_id"]),
    ("LandingCertificate", ["certificate_id"], ["certificate_id"]),
]

LINK_TYPES = [
    ("declares", "Backend", "Capability", "N:N"),
    ("certifies", "ConformanceReport", "Backend", "1:1"),
    ("trained_on", "Adapter", "Corpus", "1:N"),
    ("uses", "Agent", "Adapter", "1:1"),
    ("authorized_by", "Agent", "DelegationCertificate", "1:1"),
    ("attested_by", "Document", "Epoch", "1:1"),
    ("clears", "LandingCertificate", "Agent", "1:1"),
    ("under", "LandingCertificate", "DelegationCertificate", "1:1"),
]

ACTION_TYPES = [
    # name, effect, targets, min_tier, requires_deleg, hitl
    ("ingest_document", "create", ["Document"], "operate", True, False),
    ("register_artifact", "create", ["Adapter"], "deploy_prep", True, False),
    ("issue_landing_certificate", "issue", ["LandingCertificate"], "release", True, True),
    ("supersede_fact", "supersede", ["Document"], "curate", True, False),
]

CAPABILITIES = ["BI_TEMPORAL", "SUPERSESSION_CHAIN", "HARD_DELETE", "MULTI_TENANT", "AUDIT",
                "CONCURRENCY_CONTROL", "PROVENANCE", "CRYPTOGRAPHIC_PROVENANCE",
                "CROSS_SESSION_PROPAGATION", "CONFLICT_DETECTION"]
RESERVED = {"CROSS_SESSION_PROPAGATION", "CONFLICT_DETECTION"}


def declare_interface(wm):
    for name, props, idp in OBJECT_TYPES:
        wm.declare_object_type(name, props, idp)
    for name, frm, to, card in LINK_TYPES:
        wm.declare_link_type(name, frm, to, card)
    for name, effect, targets, tier, deleg, hitl in ACTION_TYPES:
        wm.declare_action_type(name, effect, targets, tier, requires_deleg=deleg, hitl=hitl)


def seed_world_model(wm, deleg, human_id, corpus_hash):
    """Seed the static GNS digital twin. Returns key object ids."""
    wm.add_object("Protocol", {"name": "GMP", "version": "0.2", "status": "frozen"})
    wm.add_object("Protocol", {"name": "gcrumbs", "version": "0.2", "status": "draft"})
    wm.add_object("Protocol", {"name": "GEIANT", "version": "0.2", "status": "draft"})
    caps = {c: wm.add_object("Capability", {"name": c, "state": "reserved" if c in RESERVED else "realized"})
            for c in CAPABILITIES}
    backend = wm.add_object("Backend", {"name": "PostgresGMPBackend", "declared_capabilities": 7})
    report = wm.add_object("ConformanceReport", {"subject_backend": "PostgresGMPBackend", "m8_score": 1.000})
    corpus = wm.add_object("Corpus", {"corpus_hash": corpus_hash, "n_traces": 135})
    agent = wm.add_object("Agent", {"handle": "gns-assistant@grafomem", "role": "assistant", "model_id": "byom"})
    deleg_obj = wm.add_object("DelegationCertificate",
                              {"deleg_id": deleg["deleg_id"], "human_principal": human_id, "tier": deleg["tier"]})
    for c, cid in caps.items():
        wm.add_link("declares", backend, cid)
    wm.add_link("certifies", report, backend)
    wm.add_link("authorized_by", agent, deleg_obj)
    return {"backend": backend, "report": report, "corpus": corpus, "agent": agent, "deleg_obj": deleg_obj}
