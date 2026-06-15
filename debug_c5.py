import time
from qdrant_client import QdrantClient, models
from aml.generator.workloads.w6 import generate_w6
from aml.eval.harness import run_trace
from aml.backends.qdrant_gmp import QdrantGMPBackend
from aml.backends.interface import WriteOptions, RetrieveOptions

client = QdrantClient("http://127.0.0.1:6333", timeout=60.0)
collection_name = f"gmp_debug_{time.time_ns()}"

class DebugBackend(QdrantGMPBackend):
    def __init__(self, cname):
        self._batch_fn = lambda x: [[0.1]*256 for _ in x]
        def embed(texts):
            return self._batch_fn(texts)
        super().__init__(url="http://127.0.0.1:6333", collection_name=cname, embed_fn=embed)
        self.client = client
        
    def write(self, content, options):
        options.tenant_id = "default_tenant"
        return super().write(content, options)

backend = DebugBackend(collection_name)
tr = generate_w6(seed=0)

for f in tr.facts:
    f.tenant_id = "default_tenant"
for s in tr.sessions:
    s.tenant_id = "default_tenant"

print("Running trace...")
run_trace(backend, tr, budget_tokens=512)
print("Count before snapshot:", backend.client.count(collection_name).count)

snap = backend.client.create_snapshot(collection_name)
snap_loc = f"file:///qdrant/snapshots/{collection_name}/{snap.name}"

backend.client.delete_collection(collection_name)
backend.client.recover_snapshot(collection_name, location=snap_loc)

while True:
    if backend.client.collection_exists(collection_name):
        info = backend.client.get_collection(collection_name)
        print("Status:", info.status)
        if info.status == models.CollectionStatus.GREEN:
            break
    time.sleep(0.1)

c = backend.client.count(collection_name).count
print("Count after snapshot:", c)

res = backend.retrieve("test", RetrieveOptions(tenant_id="default_tenant", budget_tokens=1000))
print("Retrieved elements count:", len(res))

backend.client.delete_collection(collection_name)
