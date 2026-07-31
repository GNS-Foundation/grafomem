# DEMO GUIDE · KEEP OPEN DURING THE PRESENTATION
## Kapwork Live Demo — Step-by-Step
GRAFOMEM Cloud on staging · five beats · ~6 minutes

All commands below are the **real, tested** commands — nothing to fill in. Run them from the
`demo/` folder after the one-time session setup in Phase A. Use `python3` (not `python`).
Test every command once before the meeting.

> **Environment.** This guide runs on **staging** (`grafomem-staging-staging.up.railway.app`) —
> that's the honest "running live on our staging Cloud, on synthetic data" frame, and it's the
> only environment the demo scripts are wired to. Production is now fully synced and live too
> (`grafomem-production.up.railway.app`) if you'd rather show the **public** pages (landing `/`,
> funder `/verify/`) on the production URL — but keep the *scripted beats* on staging so you're
> not seeding synthetic tenants into prod.

---

## Phase A — Before they join (5 min ahead)

**1. Open a terminal in the repo, large font, and set up the session once:**
```bash
cd /Users/camiloayerbeposada/grafomem/demo
export GRAFOMEM_BASE=https://grafomem-staging-staging.up.railway.app
```
> ⚠️ This `export` is mandatory. Without it the scripts default to `localhost:8090` and every
> beat fails in the room. Every command below inherits it from this shell. (Creds are handled
> automatically — `reset_demo.py` writes them to the gitignored `demo/.demo_creds.json`.)

**2. Confirm staging is healthy (full cloud layer, not just the DB):**
```bash
curl -s https://grafomem-staging-staging.up.railway.app/readyz
```
✓ Watch for: `"status":"ok"` and the checks list includes `decision_trail, governance_gateway,
orchestrator, erasure_proof, regulatory_reports`.

**3. Confirm the signing-key endpoint returns a real key:**
```bash
curl -s https://grafomem-staging-staging.up.railway.app/v1/gcrumbs/verify/key
```
✓ Watch for: `{"algorithm":"ed25519","public_key_hex":"…","public_key_b64":"…"}`, HTTP 200 —
a real key, **not** 404 and **not** all-zeros.

**4. Reset to a clean tenant (this also provisions the tenant):**
```bash
python3 reset_demo.py
```
✓ Watch for: `after: tenant … has 0 decision(s) — CLEAN slate`.

**5. Get the dashboard login for this tenant** (you'll need it for Beat 5 — the email is random
per reset, the password is fixed):
```bash
cat demo/.demo_creds.json
```
✓ Note the **`email`** value. The dashboard password is always **`demo-Kapwork-2026!`**.
(Run this from the repo root, or `cat .demo_creds.json` from inside `demo/`.)

**6. Full silent dry-run of all four scripted beats, then reset again so you start clean:**
```bash
python3 demo.py --beat all
python3 reset_demo.py
cat demo/.demo_creds.json   # re-read the NEW email after the reset
```

**7. Pre-open two browser tabs** (log in now so you're not typing a password on stage):
- `https://grafomem-staging-staging.up.railway.app/portal/` → sign in with the `email` from
  step 6 + password `demo-Kapwork-2026!`. Leave it on the **Decision Trail** view.
- `https://grafomem-staging-staging.up.railway.app/verify/` → the funder page (Beat 4 browser variant).

**8. Close noisy tabs and logs. Have this guide open on a second screen or on paper.**

---

## Phase B — Opening line (10 sec)

🗣 “Kapwork certifies invoices so funders can advance against them. Today that trust is
institutional — trust Kapwork's process. I want to show you what it looks like when each
certification carries its own verifiable evidence. This is running live on our staging Cloud,
on synthetic invoice data — it's the mechanism, working.”

> Say “synthetic data” and “the mechanism” now — setting the honest frame first makes everything
> after it land better.

---

## Phase C — The five beats

### BEAT 1 — Verify & catch fraud (~45 sec)
Run the full batch over the 8 invoices.
```bash
python3 demo.py --beat 1
```
🗣 “Eight invoices through the verification agent. Five certified, three rejected — and here's
why each was caught: Granite Peak billed Charter more than its purchase order authorized;
Blue Ridge's T-Mobile invoice has no verified approval yet; and this one's a duplicate of an
invoice already certified. The agent makes the call, with a reason.”

✓ Watch for: `Batch summary: 5 certified, 3 rejected`, and the three reject reasons —
`Invoice amount exceeds authorized PO` / `No verified approval from debtor` /
`Duplicate of already-certified invoice`.

ℹ Under the hood: the batch is submitted to `POST /v1/governed/verify-batch` — **verification
runs server-side** (a configurable rules engine), and each result is signed on our side. The
script just hands over the invoices. If asked "whose logic?": "the policy is configurable to
your rules; here it's amount ≤ PO, approval present, no duplicates."

⚠ If it goes wrong: the count is **always** 5/3 — the duplicate check runs per batch, so a
skipped reset can't change it. If a beat errors mid-way you'll see a traceback (not a wrong
count); don't debug — pivot: “let me show you that from the run I captured earlier.”

### BEAT 2 — The signed record (~30 sec)
Show one certified record and its signed receipt.
```bash
python3 demo.py --beat 2
```
🗣 “Every certified invoice produces this — a signed, time-stamped record. This is the
Receivables Report entry, and it's individually signed. Notice the signature, and the key it
was signed under.”

✓ Watch for: a `decision_record` + `execution_receipt` (defaults to the Nokia invoice) with a
visible `signature`, a `public_key` that matches the key from Phase A step 3, and a timestamp.

### BEAT 3 — Tamper-evidence (~45 sec) ★ proof moment
Verify the honest receipt, then alter one field and verify again.
```bash
python3 demo.py --beat 3
```
🗣 “Here's a certified record — it verifies clean. Now say someone tampers with it after
certification — changes a field in the record. Re-verify — it breaks. Nothing can be altered
after the fact without it showing. That's the difference between a PDF a funder has to trust and
a record that's tamper-evident.”

✓ Watch for: `honest receipt → valid: True`, then `tampered receipt → valid: False`, reason
`receipt_id mismatch — a field was altered after signing`. (On screen the altered field is
`output_hash` — “a field in the record”; don't call it a dollar amount if they're reading closely.)

### BEAT 4 — Independent funder verification (~60 sec) ★ the external climax
Fetch the public key, verify the honest receipt with it, then show a wrong key is rejected.
```bash
python3 demo.py --beat 4
```
🗣 “Now the funder. They fetch our public key — public URL, no login. And they verify the
certification themselves — no access to Kapwork, no call back to us. Valid. And if someone tries
a different key — rejected. So the check is bound to the real signer. The funder trusts it
because they checked it themselves.”

✓ Watch for: `verify with the fetched real key → valid: True`, then
`verify with a WRONG key → valid: False`.

**BEAT 4 (browser variant) — the funder page ★ strongest if you have a screen.** Open the
pre-loaded funder tab `…/verify/` and: 1. paste a certified receipt into box 1 (copy one from
Beat 2, or click **Generate a live certified receipt**), 2. **Fetch the real public key**,
3. **Verify** → big green **VALID**, 4. **Tamper one field** → **Verify** → red **INVALID**,
5. **Use a wrong key** → **Verify** → red **INVALID**.
🗣 “This is what a funder sees — a public page, no login, no access to Kapwork. Change anything,
or use the wrong key, and it fails.” (If the network is flaky, fall back to the terminal `--beat 4`.)

### BEAT 5 — The audit console / dashboard (~45 sec) ★ the internal view
Switch to the pre-loaded **`/portal/`** tab (already signed in from Phase A) and open the
**Decision Trail**.
🗣 “That was the funder's view — external, no login. This is Kapwork's own view. Every decision
the agent made — every certify and every reject — is recorded here: timestamped, queryable,
attributable. The funder verifies one certification; your team has the whole trail.”

✓ Watch for: the trail lists the decisions from Beat 1 (and any produced by beats 2–4) — each row
has a decision id, a timestamp, and the decision. Point at a **reject** row and a **certify** row.

ℹ Honest framing: this console lists **decisions** (the audit trail) — it is **not** the signed
receipt/verify experience; that's the `/verify` page from Beat 4. Say “decision trail,” not
“the receipts,” if they're reading closely.

⚠ If the login dropped or the UI is fiddly, don't fight it live — the exact same data is one
command: `curl -s -H "X-API-Key: <key from .demo_creds.json>"
https://grafomem-staging-staging.up.railway.app/v1/decisions/` → shows the count and rows. Or
just say “the trail's in the console, I'll walk you through it after.”

---

## Phase D — Close (15 sec)

🗣 “The invoice goes in, and a certification comes out that carries its own signature —
tamper-evident, verifiable by a funder who never touches your systems, and recorded in an audit
trail your team owns. That's the layer we'd build with Kapwork.”

> Then stop talking. Let them react and ask. The Q&A (separate run-sheet) has the answers to
> Pete's likely questions.

---

## Golden rules for the room

- **Never debug live.** If any beat comes back wrong or red, do NOT open the code. Say “let me
  show you that from the run I captured earlier” and move on. A calm pivot reads as composure;
  live debugging reads as fragility.
- **Frame it honestly.** “live on staging, synthetic data, the mechanism” — not “production-ready.”
- **Don't claim the governance engine.** This path records + signs. Policy engine is “the next phase.”
- **The dashboard shows decisions, not receipts.** Don't conflate the two.
- **No caching number.** It's unmeasured — don't quote it.
- **Say tamper-evident, not tamper-proof.** And “logical residency,” not “physical.”
- **A “no” is fine.** Offer them the honest verdict — it's what makes a yes real.

---

## One-line command reference

| | Command (run from `demo/`, after Phase A `export`) |
|---|---|
| Health check | `curl -s https://grafomem-staging-staging.up.railway.app/readyz` |
| Key check | `curl -s https://grafomem-staging-staging.up.railway.app/v1/gcrumbs/verify/key` |
| Reset | `python3 reset_demo.py` |
| Dashboard login (email) | `cat demo/.demo_creds.json` · password `demo-Kapwork-2026!` |
| Beat 1 — batch | `python3 demo.py --beat 1` |
| Beat 2 — record | `python3 demo.py --beat 2` |
| Beat 3 — tamper | `python3 demo.py --beat 3` |
| Beat 4 — funder verify | `python3 demo.py --beat 4` |
| Beat 5 — dashboard | open `…/portal/` (Decision Trail) |
| Dry-run all scripts | `python3 demo.py --beat all` |

**Live browser surfaces (open on a screen):**
| | URL | Login |
|---|---|---|
| Funder verification page | `…/verify/` | none (public) |
| Audit console (dashboard) | `…/portal/` | demo tenant `email` (from `.demo_creds.json`) + `demo-Kapwork-2026!` |
| Ingestion endpoint (server-side verify) | `POST /v1/governed/verify-batch` | `X-API-Key` |
| SDKs | `sdk/python` (`pip install ./sdk/python`) · `sdk/typescript` (`npm install && npm run build`) | — |

Base for all URLs: `https://grafomem-staging-staging.up.railway.app`
Setup (once per terminal): `cd /Users/camiloayerbeposada/grafomem/demo && export GRAFOMEM_BASE=https://grafomem-staging-staging.up.railway.app`

**Integration story** (if they ask “do you have an SDK / API / dashboard?”): yes to all three —
**SDK** (Python + TypeScript in `sdk/`), invoices **ingest** via `POST /v1/governed/verify-batch`,
funders **verify** via the public endpoints, and the **dashboard** is the audit console above.

---

*Internal demo guide — ULISSY s.r.l. — not for distribution.*
