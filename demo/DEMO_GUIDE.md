# DEMO GUIDE · KEEP OPEN DURING THE PRESENTATION
## Kapwork Live Demo — Step-by-Step
GRAFOMEM Cloud on staging · six beats · ~6 minutes

All commands below are the **real, tested** commands — nothing to fill in. Run them from the
`demo/` folder after the one-time session setup in Phase A. Use `python3` (not `python`).
Test every command once before the meeting.

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

**2. Confirm staging is healthy:**
```bash
curl -s https://grafomem-staging-staging.up.railway.app/readyz
```
✓ Watch for: `"status":"ok"` and the checks list includes `decision_trail, governance_gateway,
orchestrator, erasure_proof, regulatory_reports` (the full cloud layer — not just database/pool).

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

**5. Full silent dry-run, then reset again so you start the real demo clean:**
```bash
python3 demo.py --beat all
python3 reset_demo.py
```
> If you plan to use Beats 5–6 (dashboard + funder page), rehearse them now too: after this
> reset, run `python3 demo.py --beat 1`, then log into `/portal/` with the email in
> `.demo_creds.json` and confirm the 5/3 rows show; open `/verify/` and confirm it loads.
> Then `python3 reset_demo.py` once more — and remember to re-run a beat before the portal in
> the real demo so the tenant has data.

**6. Close noisy tabs and logs. Have this guide open on a second screen or on paper.**

---

## Phase B — Opening line (10 sec)

🗣 “Kapwork certifies invoices so funders can advance against them. Today that trust is
institutional — trust Kapwork's process. I want to show you what it looks like when each
certification carries its own verifiable evidence. This is running live on our staging Cloud,
on synthetic invoice data — it's the mechanism, working.”

> Say “synthetic data” and “the mechanism” now — setting the honest frame first makes everything
> after it land better.

---

## Phase C — The four beats

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

### BEAT 4 — Independent funder verification (~60 sec) ★ the climax
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

---

## Phase C-bis — The product surfaces (optional finale)

> ⚠️ **Only include Beats 5–6 if you have rehearsed them end-to-end at least twice.** The
> terminal beats (1–4) are proven; the browser surfaces are new. If the portal login or the
> grid is fiddly in rehearsal, drop these and instead say: *"the decisions also flow into the
> audit console and a funder-verification page — I'll walk you through those live once we've
> wired your data."* Honest, and keeps you on rehearsed ground.
>
> 🔑 **Tenant-match rule (critical):** `reset_demo.py` rotates to a NEW tenant on every reset.
> Log into the portal with the email in `demo/.demo_creds.json` **from the reset you are
> actually demoing from** — i.e. do NOT reset again after Beat 1–4 and before showing the
> portal, or the Decision Trail will be empty and you'll look broken. Order: reset once →
> run beats 1–4 → show portal/verify on that same tenant.

### BEAT 5 — The Audit Console (dashboard)  (~45 sec)
Show that the decisions you just created appear in the tenant's dashboard.

- Open: `https://grafomem-staging-staging.up.railway.app/portal/`
- Log in with the demo tenant's email (in `demo/.demo_creds.json`), password `demo-Kapwork-2026!`
- Go to the **Decision Trail**.

> ⚠️ **A 401 here almost always means you opened the `production` URL.** The demo tenant lives on
> **staging** — log in at `https://grafomem-staging-staging.up.railway.app/portal/`, never the
> `grafomem-production…` URL (that database has no demo tenant).

🗣 “And here it is in the product — the tenant's Audit Console. Every decision the agent made
is here: the five certified, the three rejected, each with its reason and timestamp. This is
the oversight surface your team would use.”

✓ Watch for: the Decision Trail shows the **5 certified + 3 rejected** rows for this tenant
(refresh if needed). If empty → you're logged into the wrong tenant; log in with the email from
the *current* `.demo_creds.json`.

> Note: the dashboard shows the **decisions**, not the signed receipts — the signature and
> independent verification live on the `/verify` page (Beat 6). Don't hunt for a "verify" button
> in the portal; that's by design.

### BEAT 6 — The funder verification page  (~45 sec)  ★ ties back to the climax
Show the public page a funder uses — no login, no Kapwork access.

- Open: `https://grafomem-staging-staging.up.railway.app/verify/`

🗣 “And this is what a funder uses — a public page, no login, no access to Kapwork. They paste
a certification, it fetches our public key, and it verifies the signature itself. It's the same
independent check we just ran in the terminal — now as the surface a funder actually touches.”

✓ Watch for: the page loads (public, no login) and a valid certification verifies; a tampered
one is rejected.

> If you didn't rehearse pasting a live receipt into `/verify`, keep this to *showing the page
> exists* and referring back to Beat 4 (“this is the same check, in a funder-facing page”) —
> don't attempt a first-time live paste in the room.

---

## Phase D — Close (15 sec)

🗣 “The invoice goes in, and a certification comes out that carries its own signature —
tamper-evident, and verifiable by a funder who never touches your systems. That's the layer
we'd build with Kapwork.”

> Then stop talking. Let them react and ask. The Q&A (separate run-sheet) has the answers to
> Pete's likely questions.

---

## Golden rules for the room

- **Never debug live.** If any beat comes back wrong or red, do NOT open the code. Say “let me
  show you that from the run I captured earlier” and move on. A calm pivot reads as composure;
  live debugging reads as fragility.
- **Frame it honestly.** “live on staging, synthetic data, the mechanism” — not “production-ready.”
- **Don't claim the governance engine.** This path records + signs. Policy engine is “the next phase.”
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
| Beat 1 — batch | `python3 demo.py --beat 1` |
| Beat 2 — record | `python3 demo.py --beat 2` |
| Beat 3 — tamper | `python3 demo.py --beat 3` |
| Beat 4 — verify | `python3 demo.py --beat 4` |
| Dry-run all | `python3 demo.py --beat all` |
| Beat 5 — dashboard | open `https://grafomem-staging-staging.up.railway.app/portal/` (login: email from `.demo_creds.json`, pw `demo-Kapwork-2026!`) |
| Beat 6 — funder page | open `https://grafomem-staging-staging.up.railway.app/verify/` (public, no login) |

Setup (once per terminal): `cd /Users/camiloayerbeposada/grafomem/demo && export GRAFOMEM_BASE=https://grafomem-staging-staging.up.railway.app`

---

*Internal demo guide — ULISSY s.r.l. — not for distribution.*
