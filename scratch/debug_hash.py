import json
import hashlib

canonical = '{"messages": [{"content": "[MEMORY CONTEXT \u2014 3 facts retrieved from GMP stores]\\n- GDPR Article 17 establishes the Right to Erasure, requiring data controllers to delete personal data upon request without undue delay.\\n- ISO 42001 is the first international standard for AI Management Systems, providing a framework for responsible AI governance.\\n- DORA (Digital Operational Resilience Act) requires financial entities to implement comprehensive ICT risk management frameworks, per Article 6.\\n\\n[USER QUERY]\\n\\"SSE streaming conformance test\\"", "role": "user"}], "system_prompt": "You are a compliance researcher. Your job is to retrieve relevant regulatory facts from the memory store and summarize what you find. Be precise and cite the specific articles. Keep your response under 300 words."}'

print(hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest())

