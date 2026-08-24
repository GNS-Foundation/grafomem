# Publishing `com.grafomem/cgr-capture` to the MCP Registry

Everything in this file needs Camilo's credentials or DNS access, so none of it is
automated here. Steps are ordered so **Step 1 (DNS) can start immediately and run in
parallel** with everything else — DNS propagation is the long pole.

Recon date: 2026-08-24, against the live registry docs and the published
`2025-12-11` server.json schema. The spec moves; re-check before publishing if this
sits for a while.

---

## Step 1 — DNS verification for the `com.grafomem` namespace ⟵ START NOW

The registry requires proof that we own `grafomem.com` before it will accept any
server named `com.grafomem/*`. Proof is an Ed25519 keypair: the **public** half goes
into a DNS TXT record, and `mcp-publisher` signs the publish request with the
**private** half.

**1a. Generate the keypair** (do this somewhere durable — `key.pem` becomes a
publishing credential; treat it like a deploy key and do **not** commit it):

```bash
openssl genpkey -algorithm Ed25519 -out grafomem-mcp-registry-key.pem
chmod 600 grafomem-mcp-registry-key.pem
```

**1b. Derive the TXT record value:**

```bash
PUBLIC_KEY="$(openssl pkey -in grafomem-mcp-registry-key.pem -pubout -outform DER | tail -c 32 | base64)"
echo "v=MCPv1; k=ed25519; p=${PUBLIC_KEY}"
```

**1c. Add it in Cloudflare** (grafomem.com is on Cloudflare — nameservers
`margot.ns.cloudflare.com` / `hayes.ns.cloudflare.com`):

- DNS → Records → **Add record**
- Type: **TXT**
- Name: **`@`** (the apex, `grafomem.com` — *not* a subdomain)
- Content: the full `v=MCPv1; k=ed25519; p=...` string from 1b
- TTL: Auto. Proxy status does not apply to TXT records.

The apex already has a `google-site-verification` TXT record. **Leave it.** Multiple
TXT records coexist at the apex; this is an additional record, not a replacement.

**1d. Confirm propagation** (takes a few minutes):

```bash
dig +short TXT grafomem.com | grep MCPv1
```

---

## Step 2 — Publish `grafomem-cgr` to PyPI

The registry will only list a server whose package exists on a trusted public
registry, and it verifies ownership by looking for `mcp-name: com.grafomem/cgr-capture`
in the PyPI project description. That marker is already in this package's
`README.md`, and has been confirmed present in the built wheel's `METADATA`.

The name `grafomem-cgr` was free on PyPI as of the recon.

```bash
cd packages/grafomem-cgr
rm -rf dist build src/grafomem_cgr.egg-info
python -m build --wheel --sdist
python -m twine upload dist/*
```

You will need a PyPI API token. After upload, sanity-check the marker survived:

```bash
curl -s https://pypi.org/pypi/grafomem-cgr/json | grep -c "mcp-name: com.grafomem/cgr-capture"
```

---

## Step 3 — Install `mcp-publisher`

```bash
brew install mcp-publisher
```

Or a pre-built binary:

```bash
curl -L "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_$(uname -s | tr '[:upper:]' '[:lower:]')_$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/').tar.gz" | tar xz mcp-publisher && sudo mv mcp-publisher /usr/local/bin/
```

---

## Step 4 — Log in with the domain key, then publish

Only after the TXT record from Step 1 has propagated **and** the PyPI upload in
Step 2 is live.

```bash
cd packages/grafomem-cgr

PRIVATE_KEY="$(openssl pkey -in /path/to/grafomem-mcp-registry-key.pem -noout -text | grep -A3 "priv:" | tail -n +2 | tr -d ' :\n')"
mcp-publisher login dns --domain grafomem.com --private-key "${PRIVATE_KEY}"

mcp-publisher publish
```

`mcp-publisher publish` reads `server.json` from the current directory. That file is
already written and **validated against the published `2025-12-11` schema**.

---

## Step 5 — Verify the listing

```bash
curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=com.grafomem" | python3 -m json.tool
```

---

## Notes and gotchas

- **Version coupling.** `server.json`'s `version` (`0.1.0`) and the `packages[0].version`
  must match the version actually on PyPI. Bumping the package means bumping both and
  re-running `mcp-publisher publish`.
- **Registry is in preview.** The upstream docs still carry a preview notice warning of
  breaking changes and possible data resets before GA.
- **`description` is capped at 100 characters** by the schema — the current one is 91.
  Longer copy belongs in the README, not the manifest.
- **Only `io.modelcontextprotocol.registry/publisher-provided` survives in `_meta`.** We
  don't currently set `_meta` at all.
- **Phase 2 (`com.grafomem/cgr-read`)** reuses this same DNS verification — the namespace
  is proven once, per domain, not per server. Step 1 does not need repeating.
