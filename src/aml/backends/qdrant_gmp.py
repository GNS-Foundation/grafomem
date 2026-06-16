import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
import time

import numpy as np

try:
    from qdrant_client import QdrantClient
    from qdrant_client import models
except ImportError:
    pass

from aml.backends.gmp_reference import GMP_V02_PROFILE
from aml.backends.interface import (
    Capability, CapabilityNotSupported, Memory, RetrieveOptions,
    SourceMeta, WriteOptions, MemoryBackend
)
from aml.backends.vector_only import _default_embedder
from aml.provenance import fact_id_for_content, sign_provenance

OPEN_UNTIL_TS = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc).isoformat()
FROM_BEGIN_TS = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat()


def _vec_from(dt: datetime | None) -> str:
    return dt.isoformat() if dt is not None else FROM_BEGIN_TS


def _vec_until(dt: datetime | None) -> str:
    return dt.isoformat() if dt is not None else OPEN_UNTIL_TS


def _normalize(v) -> np.ndarray:
    arr = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(arr))
    return arr / n if n > 0.0 else arr


class QdrantGMPBackend(MemoryBackend):
    __grafomem_interface__ = "0.2.0"

    def __init__(self, url: str = "http://127.0.0.1:6333", collection_name: str = "grafomem", embed_fn=None, encryption_provider=None) -> None:
        self._embed = embed_fn or _default_embedder()
        self.url = url
        self.collection_name = collection_name
        self.encryption_provider = encryption_provider
        self.client = QdrantClient(url=url, port=6333, grpc_port=6334, prefer_grpc=True, timeout=60.0)

        self._dim = int(np.asarray(self._embed("dimension probe")).shape[0])
        self._ensure_collection()

    def _ensure_collection(self):
        def _do_ensure():
            if not self.client.collection_exists(self.collection_name):
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(
                        size=self._dim,
                        distance=models.Distance.COSINE,
                    ),
                )
                # Mandatory payload indexes
                self.client.create_payload_index(self.collection_name, "tenant_id", field_schema=models.PayloadSchemaType.KEYWORD)
                self.client.create_payload_index(self.collection_name, "valid_from", field_schema=models.PayloadSchemaType.DATETIME)
                self.client.create_payload_index(self.collection_name, "valid_until", field_schema=models.PayloadSchemaType.DATETIME)
        self._retry_call(_do_ensure)

    def capabilities(self) -> set[Capability]:
        return set(GMP_V02_PROFILE)

    def storage_bytes(self) -> int | None:
        return None

    def _provenance(self, content: str, options: WriteOptions):
        if options.signing_identity is None:
            return None, None, None
        fid = fact_id_for_content(content, options.tenant_id)
        sig, pub = sign_provenance(options.signing_identity, fid)
        return pub.hex(), sig.hex(), pub.hex()

    def _retry_call(self, func, *args, **kwargs):
        import time
        for attempt in range(5):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt == 4:
                    raise
                print(f"[QdrantGMPBackend] Retry {attempt+1}/5 after error: {e}")
                # Recreate client to flush stale connection pools
                self.client = QdrantClient(url=getattr(self, 'url', "http://127.0.0.1:6333"), port=6333, grpc_port=6334, prefer_grpc=True, timeout=60.0)
                time.sleep(1.0 * (attempt + 1))

    def write(self, content: str, options: WriteOptions) -> str:
        if options.tenant_id is None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "write")

        emb = _normalize(self._embed(content)).tolist()
        written_by, signature, public_key = self._provenance(content, options)

        metadata_str = json.dumps(options.metadata or {})
        if self.encryption_provider:
            content = self.encryption_provider.encrypt(content)
            metadata_str = self.encryption_provider.encrypt(metadata_str)

        point_id = str(uuid.uuid4())
        payload = {
            "content": content,
            "written_at": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata_str,
            "valid_from": _vec_from(options.valid_from),
            "valid_until": _vec_until(None),
            "tenant_id": options.tenant_id,
            "superseded_by": None,
            "written_by": written_by,
            "signature": signature,
            "public_key": public_key
        }

        def _do_write():
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    models.PointStruct(
                        id=point_id,
                        payload=payload,
                        vector=emb,
                    )
                ],
                wait=True
            )
        self._retry_call(_do_write)
        return point_id

    def write_many(self, items: list[tuple[str, WriteOptions]]) -> list[str]:
        if not items:
            return []

        embs = self._embed([c for c, _ in items])
        if embs.ndim != 2 or embs.shape[1] != self._dim:
            raise ValueError(f"batched embedding shape {embs.shape} != (n, {self._dim})")

        now = datetime.now(timezone.utc).isoformat()
        points = []
        ids = []

        for (content, options), row in zip(items, embs):
            if options.tenant_id is None:
                raise CapabilityNotSupported(Capability.MULTI_TENANT, "write_many")

            emb = _normalize(row).tolist()
            written_by, signature, public_key = self._provenance(content, options)
            metadata_str = json.dumps(options.metadata or {})
            content_to_store = content
            
            if self.encryption_provider:
                content_to_store = self.encryption_provider.encrypt(content)
                metadata_str = self.encryption_provider.encrypt(metadata_str)

            point_id = str(uuid.uuid4())
            ids.append(point_id)

            payload = {
                "content": content_to_store,
                "written_at": now,
                "metadata": metadata_str,
                "valid_from": _vec_from(options.valid_from),
                "valid_until": _vec_until(None),
                "tenant_id": options.tenant_id,
                "superseded_by": None,
                "written_by": written_by,
                "signature": signature,
                "public_key": public_key
            }

            points.append(models.PointStruct(id=point_id, vector=emb, payload=payload))

        def _do_write_many():
            self.client.upsert(
                collection_name=self.collection_name,
                points=points,
                wait=True
            )
        self._retry_call(_do_write_many)
        return ids

    def supersede(self, old_ref: Any, content: str, options: WriteOptions) -> str:
        new_ref = self.write(content, options)
        close_at = options.valid_from or datetime.now(timezone.utc)

        def _do_update():
            res = self.client.retrieve(
                collection_name=self.collection_name,
                ids=[str(old_ref)],
                with_payload=True,
                with_vectors=True
            )
            if not res:
                return
            point = res[0]
            point.payload["valid_until"] = _vec_until(close_at)
            point.payload["superseded_by"] = new_ref
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    models.PointStruct(
                        id=str(old_ref),
                        payload=point.payload,
                        vector=point.vector
                    )
                ],
                wait=True
            )
        self._retry_call(_do_update)
        return new_ref

    def delete(self, ref: Any) -> bool:
        self._retry_call(
            self.client.delete,
            collection_name=self.collection_name,
            points_selector=models.PointIdsList(points=[str(ref)])
        )
        return True

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        if options.tenant_id is None:
            # Structurally impossible to issue unscoped query.
            return []

        qvec = _normalize(self._embed(query)).tolist()
        budget = options.budget_tokens if options.budget_tokens is not None else float("inf")

        must_filters = [
            models.FieldCondition(
                key="tenant_id",
                match=models.MatchValue(value=options.tenant_id)
            )
        ]

        if options.as_of is None:
            must_filters.append(
                models.FieldCondition(
                    key="valid_until",
                    match=models.MatchValue(value=OPEN_UNTIL_TS)
                )
            )
        else:
            as_of_iso = options.as_of.isoformat()
            must_filters.append(
                models.FieldCondition(
                    key="valid_from",
                    range=models.DatetimeRange(lte=as_of_iso)
                )
            )
            must_filters.append(
                models.FieldCondition(
                    key="valid_until",
                    range=models.DatetimeRange(gt=as_of_iso)
                )
            )

        filter = models.Filter(must=must_filters)
        limit = max(1, min(4096, budget + 1)) if budget != float("inf") else 4096

        def _do_retrieve():
            return self.client.query_points(
                collection_name=self.collection_name,
                query=qvec,
                query_filter=filter,
                limit=limit,
                with_payload=True
            )
        res = self._retry_call(_do_retrieve)

        out = []
        used = 0
        for hit in res.points:
            content = hit.payload["content"]
            if used + len(content) > budget:
                break
            out.append(self._payload_to_memory(str(hit.id), hit.payload))
            used += len(content)
        return out

    def audit(self) -> Iterator[Memory]:
        offset = None
        while True:
            res, offset = self.client.scroll(
                collection_name=self.collection_name,
                limit=100,
                offset=offset,
                with_payload=True
            )
            for p in res:
                yield self._payload_to_memory(str(p.id), p.payload)
            if offset is None:
                break

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.client.close()

    def _payload_to_memory(self, ref: str, payload: dict) -> Memory:
        sig = payload.get("signature")
        pub = payload.get("public_key")
        vf = datetime.fromisoformat(payload["valid_from"]) if payload.get("valid_from") != FROM_BEGIN_TS else None
        vu = datetime.fromisoformat(payload["valid_until"]) if payload.get("valid_until") != OPEN_UNTIL_TS else None

        content = payload["content"]
        metadata_str = payload.get("metadata", "{}")
        if self.encryption_provider:
            try:
                content = self.encryption_provider.decrypt(content)
                metadata_str = self.encryption_provider.decrypt(metadata_str)
            except Exception:
                pass  # Fallback if decryption fails or wasn't encrypted

        md = json.loads(metadata_str)

        return Memory(
            ref=ref,
            content=content,
            written_at=datetime.fromisoformat(payload["written_at"]),
            metadata=md,
            valid_from=vf,
            valid_until=vu,
            tenant_id=payload.get("tenant_id"),
            superseded_by=payload.get("superseded_by"),
            source=SourceMeta(
                write_id=ref,
                written_at=datetime.fromisoformat(payload["written_at"]),
                written_by=payload.get("written_by"),
                signature=bytes.fromhex(sig) if sig else None,
                public_key=bytes.fromhex(pub) if pub else None,
            )
        )
