"""CSO as an MCP resource type (RFC 0002 §10 / SPEC-1.0 §3)."""
from .cso import CSO
from cryptography.hazmat.primitives.asymmetric import ed25519

MCP_CONTENT_TYPE = "application/vnd.grafomem.cso+gfm"
def to_mcp_resource(cso: CSO, private_key: ed25519.Ed25519PrivateKey) -> dict:
    return {"contentType": MCP_CONTENT_TYPE, "bytes": cso.to_gfm(private_key),
            "descriptor": {"model_id": cso.model_id, "capabilities": sorted(cso.capabilities),
                           "consent": cso.consent, "hash": cso.content_hash()[:16]}}
def from_mcp_resource(res: dict, trusted_keys: dict[str, ed25519.Ed25519PublicKey]) -> CSO:
    if res["contentType"] != MCP_CONTENT_TYPE: raise ValueError("wrong content type")
    return CSO.from_gfm(res["bytes"], trusted_keys)
