# 05 — World-Model Interface Specification (`wm/0.1`)

**Status:** draft · open (MIT) · GRAFOMEM v3.0
**Companion:** `04-landing-certificate.md`
**Candidate location on graduation:** `docs/05-world-model-interface.md`

R5 governs the **HOW**: a typed world-model over the GMP fact substrate. It is
**ontology-agnostic but ontology-governed** — exactly as GMP is *embedding-agnostic*.
The interface specifies the *declaration shape* and the *governance/conformance
obligations*; it does not mandate a particular ontology. A bring-your-own ontology
declares its types against this interface and is certified by passing the obligations.

## Declaration shape

```
ObjectType {
  name        : str
  properties  : [ { name, type, required } ]
  maps_from   : FactPattern        # which GMP fact (predicate/subject/object) shape yields instances
  identity    : [property]         # content-derived, tenant-scoped (BLAKE2b-128 over identity props)
}

LinkType {
  name        : str
  from_type   : ObjectType.name
  to_type     : ObjectType.name
  cardinality : "1:1" | "1:N" | "N:N"
}

ActionType {                        # the governed write-path — the differentiator
  name           : str
  effect         : "create" | "supersede" | "delete" | "seal" | "issue"
  targets        : [ObjectType.name | LinkType.name]
  authority {
    min_tier        : str          # TierGate minimum
    requires_deleg  : bool         # a valid GEIANT delegation is mandatory
    hitl            : bool         # human-in-the-loop gate (e.g. issue / admin actions)
  }
  gateway_policy  : policy_ref      # evaluated by the Governance Gateway (PDP/PEP)
  recording       : "gcrumbs"       # every invocation emits a signed breadcrumb
}
```

## Conformance obligations ("supports X" = "passes X", not "declares X")

| Type | Obligation | Two-sided test |
|---|---|---|
| `ObjectType` | every instance is typed; identity is content-derived + tenant-scoped | typed instance accepted; untyped / cross-tenant-colliding instance rejected |
| `LinkType` | endpoints are declared types; no dangling links | valid link accepted; link to an undeclared/missing endpoint rejected |
| `ActionType` | **no write to the world-model except via a declared, governed ActionType** | authorized invocation (valid delegation, sufficient tier, policy-allowed) **succeeds + is recorded**; unauthorized invocation **is denied + is recorded** |

The `ActionType` obligation is the crypto-governed-action differentiator made testable:
it is gate **G4** (Phase 1), and any conformant world-model must pass it in **both**
directions, because one-sided success is the claims-but-leaks failure mode the GRAFOMEM
benchmark exists to catch.

The reference implementation is `grafomem_landing.worldmodel.WorldModel`
(`execute_action` = the governed write-path; `validate` = the structural obligation, gate
G3). The shipped GNS starter ontology — GNS modelling itself — is in
`conformance/seed_gns.py` (11 Object Types, 8 Link Types, 4 Action Types).
