---
status:        accepted
decision_date: 2026-09-08
record_date:   2026-09-08
provenance:    found-writing-the-conformance-corpus-before-any-implementation (spec-first, same order as v4)
scope:         cgr.cosign.v1 (§5.1, §8.3a) — PROPOSED spec, amended before its first corpus
---

# 0010 — `cgr.cosign.v1` predicate-unresolved must REJECT, not pass-with-a-warning

## Context

`cgr.cosign.v1` (§5.1) lets a profile make the approver signature **REQUIRED when a predicate over
`content_body` holds** — the mechanism that expresses "high-risk updates need a human" *from inside the
signed record* (its own §5.1 argument: a validity-affecting fact must be verifiable from the record, the
same principle the spec uses to reject profile-routing). It is the one predicate the envelope exists to
enforce.

Writing the conformance corpus **before any implementation** — the order `v4` followed, and the order
that let `v4`'s corpus catch a spec error — surfaced a fail-open in exactly that predicate.

**The finding.** As written, field-path resolution **fails open**. §5.1: *"a path that is absent or
resolves to a non-scalar makes the predicate **false** … but a verifier MUST surface
`predicate_unresolved`."* §8.3a echoes it: *"Surface `predicate_unresolved` … (treated as
not-holding)."* So an **absent, non-scalar, or null** `required_when.field` makes the predicate false ⇒
the approver signature becomes **optional** ⇒ a record with **no approver signature PASSES**, with
`predicate_unresolved` merely *surfaced*. **A misspelled predicate path on a high-risk profile therefore
removes the approval requirement while the record looks conformant** — a silent downgrade of the exact
control §5.1 was written to make un-downgradeable.

**The three-way divergence the corpus arbitrated.** The value of writing the corpus first is that three
sources that should have agreed did not, and nothing had forced the contradiction into the open:

1. **the spec text (§5.1 / §8.3a)** — unresolved ⇒ predicate false ⇒ requirement **optional** (fail
   *toward* not-requiring approval);
2. **the instruction that commissioned the corpus** — described these same cases as *"fail closed"*;
3. **§8's own preamble** — *"failing closed on the first failure,"* of which §8.3a is, as written, the
   **sole exception.**

Three readings, three directions. The corpus existed to arbitrate precisely this, and did.

## Decision

**An unresolvable required-ness predicate MUST REJECT.** On `predicate_unresolved` (an absent,
non-scalar, or null `required_when.field`), the verifier treats the required-ness as **UNDETERMINED**,
and an undetermined required-ness is **not a basis for passing**: the record is rejected with
`predicate_unresolved` as the **reason**. `predicate_unresolved` becomes a **rejection reason, not a
surfaced warning.** This brings §8.3a into line with §8's own "failing closed on the first failure,"
removing its sole exception.

The amendment threads through **both** the predicate definition and the verifier obligations so they
agree:

- **§5.1** — the bullet that currently makes an absent/non-scalar path make the predicate *"false"* is
  changed: an unresolvable path does not silently make the predicate false; it makes the requirement
  **undetermined**, which the verifier MUST reject (§8.3a).
- **§8.3a** — its disposition changes from *"surface `predicate_unresolved` (treated as not-holding)"*
  to *"if the predicate is unresolvable, **reject** with reason `predicate_unresolved`."*

Also in this amendment (unrelated to the fail-open, but a validity-affecting-fact-in-the-signed-body
point found in the same pass): a **one-line note to §5** that a `free`-mode profile's referenced-record
hashes live in **`content_body`** (signed), **never** in the excluded `evidence_ref` envelope key — so a
profile author cannot externalise a validity-affecting reference into a key the system signature does not
cover. Same principle as §5.1 and `v4` §2.4.

## Consequences

**The trade-off, and the choice.**

- **CHOSEN — reject-on-unresolved.** A profile-authoring error (a misspelled or mistyped `required_when`
  path) surfaces **at the first record, loudly**, as a rejection — not laundered through a
  present-but-meaningless absence of a signature.
- **REJECTED — treat-unresolved-as-REQUIRED, then reject only if unsigned.** This also closes the
  fail-open, but it accepts a record whenever an approver signature happens to be present, even though
  *which* condition the signature was meant to satisfy could not be evaluated — a present signature would
  paper over the authoring error instead of exposing it. The requirement must be **determinable**, not
  merely **satisfiable-by-any-signature**.

**Cost of the choice, stated plainly.** A malformed `required_when` path **DoSes that profile's records
until the profile is fixed** — every record under it rejects. This is accepted: the failure is
**visible** (rejection with `predicate_unresolved`, not a silent pass), **one-directional** (friction
that blocks records, never a silent admission), and **bounded to the misconfigured profile**. Friction
until fixed is the correct direction for a control that gates human approval; silent insecurity is not.

**The principle it restores.** §5.1's own argument: *a validity-affecting fact must be verifiable from
the record.* Whether approval was owed is validity-affecting; if the predicate that decides it cannot be
evaluated, the record's validity is **unknown**, and an unknown-validity record must not pass. The
pre-amendment behaviour let an unevaluable requirement resolve to "not owed" — the profile-routing
silent-downgrade the spec rejects, reintroduced one layer down.

**Corpus.** The corpus (next PR) encodes the **amended** behaviour: vectors `U1`–`U3` (absent /
non-scalar / null `required_when.field`) expect **reject**, reason `predicate_unresolved`. Had the corpus
been generated against the un-amended spec, it would have baptised the fail-open as "conformance."

## Open questions

- Whether `predicate_unresolved` should carry the offending field-path in the rejection reason for
  operability (proposed: yes — it turns a DoS into a one-line fix). Non-normative; left to the verifier.
- The other three corpus interpretations recorded in the approved report stand as proposed, not decided
  here: the pinned **test profile registry** (§5 registry document is unwritten — the `[OPEN]` in §5/§11;
  the corpus fixture must match it when written), §8.9 free-mode **degrade-vs-resolve** (corpus takes the
  spec's proposed degrade default, tagged to flip), and Q1/Q4's flagged content-digest and
  `agent_draft_digest` choices. This record resolves **only** the §5.1/§8.3a fail-open.
