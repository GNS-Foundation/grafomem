# Key Custody & Secrets Management

**GRAFOMEM Cloud Edition**

## Production Requirement
The target architecture for Grafomem production environments mandates the use of a true Key Management Service (KMS) or Hardware Security Module (HSM) for handling cryptographic signing and encryption operations.

**In Phase 0, KMS/HSM is NOT implemented.** The currently provided solution (`EnvIdentity`) is a documented-custody fallback intended strictly for validation, development, and early deployment until KMS integration is finalized.

## Phase 0: Environment-Backed Identity

The application reads cryptographic secrets directly from the environment (e.g., via a `.env.prod` file or container secrets).

### 1. Erasure Certificates (`ERASURE_SIGNING_KEY`)
- **Type:** 32-byte Ed25519 private seed (hex encoded)
- **Purpose:** Cryptographically signing erasure certificates (`GRAFOMEM_ERASURE_V1`) during a hard-delete.
- **Constraints:** This key binds the tenant's deletion authority. It is generated securely via `bin/generate_prod_keys.py`. The interface guarantees the key is never exported; payloads are passed in and signatures returned.

### 2. Provider API Keys (`PROVIDER_ENCRYPTION_KEY`)
- **Type:** 32-byte URL-safe base64-encoded Fernet key (or comma-separated list of keys for rotation)
- **Purpose:** Encrypting tenant LLM API keys (e.g., OpenAI, Anthropic) at rest in the PostgreSQL database.
- **Constraints:** The key MUST be securely generated independently of any database URLs or application configs.

## Rotation Procedure

For `PROVIDER_ENCRYPTION_KEY`:
1. Generate a new Fernet key.
2. Prepend the new key to the comma-separated `PROVIDER_ENCRYPTION_KEY` string. The first key is used for encryption, while all keys are valid for decryption.
3. Over time, decrypt and re-encrypt all stored secrets to deprecate the old key.

For `ERASURE_SIGNING_KEY`:
Key rotation requires invalidating previous public keys on the client or maintaining a registry of active public keys bound to the tenant.

*Disclaimer: Real key management services (AWS KMS, Google Cloud KMS, HashiCorp Vault) should replace this file-based local custody model before full enterprise General Availability.*
