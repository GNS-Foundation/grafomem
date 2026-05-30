# 04 — Landing Certificate Specification (`lc/0.1`)

**Status:** draft · open (MIT) · GRAFOMEM v3.0
**Companion:** `05-world-model-interface.md`
**Candidate location on graduation:** `docs/04-landing-certificate.md`

The Landing Certificate is the single object a regulated enterprise shows to prove
*what* it deployed, *from where*, *under whose authority*, *cleared how*, and *what it
may do*. It is a signed, gcrumbs-anchored sibling of the Erasure Certificate and the
Conformance Report, reusing the platform's existing crypto: BLAKE2b-128 IDs,
BLAKE2b-256 content/Merkle hashes, Ed25519 signatures, and gcrumbs Merkle epochs with
O(log N) inclusion proofs.

## Schema

```
LandingCertificate {
  version            : "lc/0.1"
  certificate_id     : BLAKE2b-128( tenant_id ∥ artifact_ref ∥ data_provenance.merkle_root
                                     ∥ authority.delegation_ref ∥ conformance.result ∥ timestamp )   # 0x1F sep
  tenant_id          : str
  timestamp          : float (epoch seconds)

  artifact {                                  # R1 — WHAT
    artifact_ref     : str                    # OCI+ModelPack reference
    manifest_digest  : BLAKE2b-256
    base_model_ref   : str                    # first-class — closes the "which base model?" gap
    layer_hashes     : [BLAKE2b-256]          # adapter delta, prompts, retrieval-config
    kind             : "rag" | "lora" | "lora+rag" | "prompt-config"
  }

  data_provenance {                           # R2 — FROM WHERE
    corpus_hash      : BLAKE2b-256            # the KB corpus (e.g. grafomem-bench-v0.2.0)
    epoch_id         : str                    # gcrumbs epoch sealing the ingestion breadcrumbs
    merkle_root      : BLAKE2b-256
    source_leaf      : BLAKE2b-256            # a sealed ingestion breadcrumb leaf
    inclusion_proof  : [{hash, right}]        # O(log N) — source_leaf ∈ merkle_root
    composition_ref  : str | null            # R4 composition record (Phase 2+)
  }

  authority {                                 # GEIANT — UNDER WHOSE
    delegation_ref       : str                # GEIANT Delegation Certificate id
    human_principal      : str                # the rooted human
    trust_tier           : str                # TierGate level
    delegation_sig       : Ed25519(hex)       # signs delegation_signed_body
    delegation_signed_body : str              # the canonical delegation body the human signed
  }

  conformance {                               # R3 — CLEARED HOW
    harness_version  : str
    result           : "pass"                 # issuance requires pass
    per_policy       : { policy_id : "pass" | "fail" }
  }

  permitted_actions  : [ActionType.name]      # R5 — MAY DO WHAT (once deployed)

  anchor {                                     # gcrumbs — tamper-evidence
    epoch_id         : str
    merkle_root      : BLAKE2b-256
  }                                            # the proof + cert_leaf travel with the chain

  signer_public_key  : Ed25519 pubkey (hex)
  signature          : Ed25519(hex) over canon(all fields except signer_public_key/signature)
}
```

## Offline verification procedure

Given **only** {certificate, gcrumbs chain, public keys, artifact bytes} — no live service:

1. Verify `signature` against `signer_public_key` over the canonical signed body.
2. Recompute `artifact.layer_hashes` from the artifact bytes; all must match.
3. Verify `data_provenance.inclusion_proof` of `source_leaf` against `data_provenance.merkle_root` → the source set was sealed.
4. Verify `authority.delegation_sig` over `authority.delegation_signed_body` against the human principal's public key → authority roots in a human at the stated tier.
5. Confirm `conformance.result == "pass"`.
6. Verify the certificate's own inclusion proof (its `cert_leaf` ∈ `anchor.merkle_root`) → the certificate is sealed and tamper-evident.

All six pass ⇒ the verifier has reconstructed, provably and offline, **what / from where /
under whom / cleared how / may do what**. This procedure is gates **G6** (Phase 1) and
**GP5** (Phase 2) of the conformance suite. The reference verifier is
`grafomem_landing.certificate.verify_landing_certificate`; the adversarial tests confirm
it rejects a tampered artifact, a forged authority, and a faked provenance root.
