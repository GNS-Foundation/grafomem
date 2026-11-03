from typing import Any, Optional, Tuple
from cryptography.hazmat.primitives.asymmetric import ed25519
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from grafomem.cso import CSO

from .thread_keys import (
    ThreadKeyManager,
    current_thread_id,
    ThreadContextMissing,
)


class GrafomemSerializer(SerializerProtocol):
    """
    Layer 1: Serializer decorator for LangGraph checkpointers.
    Wraps the underlying serde to output signed .gfm blobs.

    When a `key_manager` is supplied, checkpoint payloads are additionally encrypted under a
    per-thread key (crypto-erasure, opt-in): the inner bytes are Fernet-encrypted before being
    wrapped in the signed CSO, and `meta["enc"]="fernet"` marks the blob. `compute_content_hash`
    is deliberately left over the *plaintext* bytes, so the receipt's content binding and
    LangGraph's keying are unaffected by encryption. Without a `key_manager`, behavior is
    unchanged (signed, not encrypted) and old plaintext checkpoints still load.
    """

    def __init__(
        self,
        private_key: ed25519.Ed25519PrivateKey,
        inner: SerializerProtocol | None = None,
        key_id: str = "grafomem_checkpoint",
        trusted_keys: dict | None = None,
        model_id: str = "grafomem-langgraph-v1",
        capabilities: frozenset[str] | None = None,
        key_manager: Optional[ThreadKeyManager] = None,
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
        self.key_manager = key_manager

    def _create_cso(self, raw_bytes: bytes, meta: dict | None = None) -> CSO:
        return CSO(
            M=None,
            blob=raw_bytes,
            payload_type="blob",
            model_id=self.model_id,
            capabilities=self.capabilities,
            key_id=self.key_id,
            consent={"subject_id": "langgraph-checkpoint", "policy": "tenant"},
            meta=meta if meta is not None else {"provenance": None, "version": "1.0.0", "tags": []},
        )

    def compute_content_hash(self, obj: Any) -> str:
        """Content hash over the PLAINTEXT serialized bytes — unaffected by encryption."""
        _, raw_bytes = self.inner.dumps_typed(obj)
        cso = self._create_cso(raw_bytes)
        return cso.content_hash()

    def dumps_typed(self, obj: Any) -> Tuple[str, bytes]:
        type_, raw_bytes = self.inner.dumps_typed(obj)

        tid = current_thread_id.get()
        if self.key_manager is not None and tid is not None:
            # Encrypt the inner bytes under the thread's DEK, then wrap+sign the ciphertext.
            dek = self.key_manager.get_key(tid)
            ciphertext = dek.encrypt(raw_bytes)
            fp = self.key_manager.fingerprint(tid)
            meta = {"provenance": None, "version": "1.0.0", "tags": [], "enc": "fernet", "enc_fp": fp}
            cso = self._create_cso(ciphertext, meta=meta)
        else:
            cso = self._create_cso(raw_bytes)

        gfm_bytes = cso.to_gfm(self.private_key)
        return (type_, gfm_bytes)

    def loads_typed(self, data: Tuple[str, bytes]) -> Any:
        type_, gfm_bytes = data

        # Verify and unwrap (signature checked here, before any interpretation).
        cso = CSO.from_gfm(gfm_bytes, self.trusted_keys)

        if cso.payload_type != "blob" or cso.blob is None:
            raise ValueError("GrafomemSerializer expected a blob payload")

        blob = cso.blob
        if cso.meta.get("enc") == "fernet":
            if self.key_manager is None:
                raise ValueError(
                    "encrypted checkpoint but this GrafomemSerializer has no key_manager configured"
                )
            tid = current_thread_id.get()
            if tid is None:
                raise ThreadContextMissing(
                    "encrypted checkpoint read without a thread_id in scope "
                    "(e.g. a cross-thread list); list per thread_id instead"
                )
            dek = self.key_manager.get_key(tid)  # raises ThreadKeyDestroyed if crypto-erased
            blob = dek.decrypt(blob)

        return self.inner.loads_typed((type_, blob))
