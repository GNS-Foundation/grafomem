from typing import Any, Tuple
from cryptography.hazmat.primitives.asymmetric import ed25519
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from grafomem.cso import CSO

class GrafomemSerializer(SerializerProtocol):
    """
    Layer 1: Serializer decorator for LangGraph checkpointers.
    Wraps the underlying serde to output signed .gfm blobs.
    """

    def __init__(
        self,
        private_key: ed25519.Ed25519PrivateKey,
        inner: SerializerProtocol | None = None,
        key_id: str = "grafomem_checkpoint",
        trusted_keys: dict | None = None,
        model_id: str = "grafomem-langgraph-v1",
        capabilities: frozenset[str] | None = None
    ):
        self.private_key = private_key
        self.inner = inner or JsonPlusSerializer()
        self.key_id = key_id
        
        if trusted_keys is None:
            self.trusted_keys = {self.key_id: private_key.public_key()}
        else:
            self.trusted_keys = trusted_keys
            
        self.model_id = model_id
        self.capabilities = capabilities or frozenset(["namespace.checkpoint"])
        
    def _create_cso(self, raw_bytes: bytes) -> CSO:
        return CSO(
            M=None,
            blob=raw_bytes,
            payload_type="blob",
            model_id=self.model_id,
            capabilities=self.capabilities,
            key_id=self.key_id,
            consent={"subject_id": "langgraph-checkpoint", "policy": "tenant"}
        )

    def compute_content_hash(self, obj: Any) -> str:
        """Computes the content_hash without signing."""
        _, raw_bytes = self.inner.dumps_typed(obj)
        cso = self._create_cso(raw_bytes)
        return cso.content_hash()
        
    def dumps_typed(self, obj: Any) -> Tuple[str, bytes]:
        type_, raw_bytes = self.inner.dumps_typed(obj)
        
        # Wrap as a blob CSO
        cso = self._create_cso(raw_bytes)
        
        gfm_bytes = cso.to_gfm(self.private_key)
        return (type_, gfm_bytes)

    def loads_typed(self, data: Tuple[str, bytes]) -> Any:
        type_, gfm_bytes = data
        
        # Verify and unwrap
        cso = CSO.from_gfm(gfm_bytes, self.trusted_keys)
        
        if cso.payload_type != "blob" or cso.blob is None:
            raise ValueError("GrafomemSerializer expected a blob payload")
            
        return self.inner.loads_typed((type_, cso.blob))
