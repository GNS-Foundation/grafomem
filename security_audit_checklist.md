# Grafomem Security Audit Checklist

## 1. Encryption & Data Protection
- `[x]` **Envelope Encryption:** Each tenant uses a unique Data Encryption Key (DEK), generated via Fernet.
- `[x]` **Key Wrapping:** DEKs are wrapped using the `GRAFOMEM_MASTER_KEY` (Key Encryption Key) before being stored.
- `[x]` **Encryption at Rest:** `memories.content`, `decision_records.*`, and `orchestrator_steps` payloads are encrypted before being persisted to the database.
- `[x]` **Cross-Node Cache Invalidation:** Crypto-erasing a DEK synchronously propagates an invalidation event across all cluster nodes using `pg_notify`.

## 2. Access Control (RBAC)
- `[x]` **Least Privilege Scopes:** API keys are issued with strict, delimited scopes (e.g., `memory:read`, `orchestrator:admin`).
- `[x]` **Tenant Isolation:** Every request is strictly bound to a `TenantContext`. All vector index queries are pre-filtered on `tenant_id`.
- `[x]` **LLM Key Protection:** Read and rotation access to `PROVIDER_ENCRYPTION_KEY` and LLM registry routes are gated by the `llm:admin` scope.
- `[x]` **Escalation Prevention:** Creating new keys requires the `keys:admin` scope, preventing read-only tokens from granting themselves higher privileges.

## 3. Data Residency & GDPR
- `[x]` **Geographic Boundary Enforcement:** The `region` field is mapped as a top-level schema column to ensure HNSW vector indexes enforce data residency boundaries directly at the index level.
- `[x]` **Right-to-be-Forgotten (Crypto-Erasure):** Wiping a tenant's DEK renders all ciphertext cryptographically inaccessible, achieving immediate compliance without waiting for garbage collection.
- `[x]` **Orphaned Embedding Sweeper:** Asynchronous background tasks scan for and hard-delete unlinked vector embeddings within strict SLA windows.

## 4. Observability & Auditing
- `[x]` **Immutable SIEM Logs:** `governance_logs` and `gcrumbs` access events are forwarded to the SIEM Exporter with enforced 180-day retention policies.
- `[x]` **Exception Sanitization:** HTTP 500 error boundaries intercept exceptions and return generic messages to prevent internal stack traces or environment variables from leaking to the client.

## 5. Defense against LLM Exploits
- `[x]` **Tool Execution Governance:** The PDP/PEP architecture enforces a strict `TOOL_DENY` native policy. Untrusted agent tools cannot execute without passing policy bounds.
- `[x]` **Loop & Timeout Protection:** The orchestrator strictly limits execution steps and time-in-flight to mitigate resource exhaustion attacks (e.g., prompt injections attempting to trigger infinite loops).
