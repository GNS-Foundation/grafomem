from grafomem.cso import CSO
h1 = CSO(M=None, blob=b"A", payload_type="blob", model_id="v1", key_id="k").content_hash()
h2 = CSO(M=None, blob=b"B", payload_type="blob", model_id="v1", key_id="k").content_hash()
print(f"CSO(b'A') hash: {h1}")
print(f"CSO(b'B') hash: {h2}")
print(f"Match? {h1 == h2}")
