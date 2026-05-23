# GRAFOMEM — Canonical Finding Registry (design draft)

| | |
|---|---|
| **Fixes** | Gap #1 (F-number collision between the paper and the W7–W9 cards) |
| **Feeds** | WS3 `src/aml/eval/findings.py`; gated by WS6 CI |
| **Status** | Design draft — reference seed (`findings.toml`) and validator (`findings.py`) attached and tested |

## 1. The problem

F-numbers are currently hand-assigned in two places that have drifted apart. The
repo's cards use a global sequence — verified: W1→F1–F2, W2→F3–F5, W3→F6–F7,
W4→F8–F9, W6→F10–F11, W5→F12–F13 — so the v0.2 cards for W7–W9 occupy **F14–F18**.
The paper, edited separately, numbered W10's two findings **F14/F15**. Same numbers,
different findings. Any document that later cites both is now ambiguous, and nothing
stops the next workload from colliding again.

The cause is that the *number* is authored in prose, in more than one file. The fix
is to make the number a piece of **data with one owner**, and to give it the same
guarantees the corpus already gives a trace hash.

## 2. Principle: a finding number is an identifier, and identifiers are durable

GRAFOMEM already has a discipline for stable identifiers — the corpus lock. A
trace hash is immutable, the per-workload rollups are stable under suite growth, and
adding a workload only ever *appends*. Finding numbers get the identical treatment:

- **Immutable** — once `F4` is published it binds permanently to one finding. Its
  measured *value* may be re-taken (stub → BGE), but the number never rebinds to a
  different finding.
- **Append-only** — a new finding takes `max(existing) + 1`. Existing numbers never
  shift when a workload is added, exactly as existing rollups stay byte-identical
  when the corpus grows.
- **Durable under withdrawal** — a retracted finding is `status = "retired"`, a
  tombstone that holds its slot forever. Numbers are never reused. (This is the same
  guarantee W10 itself tests on deletes — a committed delete must not resurrect; here
  a retired *number* must not resurrect as a different finding.)
- **Single source of truth** — one registry file. The paper's Appendix C, the
  `results/W*-finding.md` cards, and the `ComplianceReport` are **projections**
  emitted from it. No F-number is authored anywhere else.

## 3. The registry — `findings.toml`

A flat, human-readable, content-light registry (companion to `corpus.toml`). One
record per finding:

| field | meaning |
|---|---|
| `id` | `F4` — the immutable identifier |
| `workload` | `W2` — owning workload |
| `metric` | `M1` / `M7` / `M8` / `leakage` / … — what the finding reports |
| `status` | `confirmed` · `provisional` · `needs-reconfirm` · `retired` |
| `embedder` | `bge-small-en-v1.5` · `stub` · `deterministic` · `invariant` |
| `producer` | `scripts/run_w2.py` — the script that regenerates it |
| `headline` | the one-line card/Appendix-C text |

`status` and `embedder` carry the freshness signal: a finding produced under the
stub, or before the current corpus, is `needs-reconfirm` until the BGE pass (WS5)
re-confirms it. That makes the stub→BGE backlog and the v0.1.8→v0.2.0 re-pin
queryable rather than tribal knowledge.

`identity()` hashes only `id|workload|metric` — the *binding*, not the value — so a
re-measurement (updating a headline number after BGE) is allowed and does **not**
trip the append-only gate, while rebinding a number to a different finding does.

## 4. The lock and the gate — `findings.lock` + `findings.py validate`

A `findings.lock` freezes `{id: identity()}` for every published number, committed
alongside the registry. The gate diffs registry against lock and rejects anything
that is not an addition — the same pre-commit workflow used for `corpus.lock`:

```
validate(findings, lock) enforces:
  1. no duplicate numbers          → catches the F14/F15 collision
  2. contiguous 1..N (tombstones allowed, not deletions)
  3. append-only vs lock           → no published id removed or rebound
```

Tested against the attached seed (`python findings.py`):

```
1) validate the seed:        CLEAN — next free id is F21
2) inject W10 = F14/F15:      VIOLATION: duplicate number F14 ('W7' and 'W10')
                              VIOLATION: duplicate number F15 ('W7' and 'W10')
3) re-home a published id:    VIOLATION: F4 identity changed — rebound (forbidden)
4) freshness:                 needs re-confirm: F14, F15, F16, F17, F18
```

The collision the registry exists to prevent is caught mechanically, not by review.

## 5. Projections — what consumers render

Nobody downstream invents a number; they ask the registry for the slice they need.

- **Paper Appendix C** — `emit_appendix_c(include={W1..W6, W10})` renders F1–F13
  then F19–F20. The F14–F18 gap is *correct*: a document may omit workloads it does
  not analyze. A gap in a document is fine; a gap in the registry is a bug.
- **Cards** — `emit_card("W7")` renders `results/W7-finding.md` from the registry,
  so a card can never disagree with the canonical number.
- **WS3 `ComplianceReport`** — `report.findings` is `emit_report_findings(registry,
  run)`: the findings relevant to the capabilities the run exercised, each with its
  canonical id, status, and producing embedder.

Because every surface is a projection, the WS6 CI gate can additionally assert that
the committed paper table and cards **equal** the registry's emitted output — they
cannot drift.

## 6. Migration — closing the current inconsistency

1. **Seed** `findings.toml` from today's canonical assignment: F1–F13 (W1–W6, from
   the paper, verified) and F19–F20 (W10). The attached seed is exactly this.
2. **Transcribe** the W7–W9 cards into F14–F18 (placeholders are in the seed marked
   `TRANSCRIBE`), keeping `status = needs-reconfirm` until WS5.
3. **Re-point the paper.** The Appendix C / §5.2 / §6 references I set to **F14/F15**
   for W10 become **F19/F20**. (Three string edits — I can do these in the same pass
   that fills the Appendix B hashes you're sending.)
4. **Lock + gate.** Commit `findings.lock`; add `python findings.py --check` to the
   WS6 CI alongside the corpus R3 check.

## 7. How it slots into the product plan

- **WS3** — this *is* `findings.py`. It replaces "auto-generate F-numbers from the
  §10 template" (which would re-introduce ad-hoc numbering) with registry-backed
  assignment: `next_id()` hands out numbers, `emit_*` renders every surface.
- **WS6** — `validate` is a CI gate with the same standing as the corpus determinism
  check: a PR that collides, rebinds, or silently deletes a finding number fails.
- **03-eval-metrics.md §10** — the finding-card *template* stays; the *numbering* it
  describes now points at the registry as authority.

## 8. Open choices

- **Card storage**: keep `results/W*-finding.md` as committed projections (regenerated
  + diff-checked in CI), or generate them on demand and stop committing them?
  Recommend keeping them committed — they're useful at rest and the CI equality check
  makes drift impossible either way.
- **Value-in-registry**: store the headline *number* (e.g. `value = 0.867`) as a
  typed field for machine consumption, or keep the prose headline as the only
  payload? Recommend adding optional typed fields (`value`, `delta`, `budget`) so the
  `ComplianceReport` can compare runs numerically without parsing prose. Cheap, and
  it makes "needs-reconfirm" checkable against a fresh run, not just a status flag.
