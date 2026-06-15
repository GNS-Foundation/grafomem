import os
import asyncio
from aml.cloud.llm_registry import LLMRegistry, LLMRequest, LLMProvider
from aml.cloud.identity import EnvIdentity
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

def main():
    os.environ["OPENAI_API_KEY"] = "sk-PLATFORM-MASTER-KEY-DO-NOT-LEAK"
    
    priv = Ed25519PrivateKey.generate()
    raw_seed = priv.private_bytes(encoding=Encoding.Raw, format=PrivateFormat.Raw, encryption_algorithm=NoEncryption())
    os.environ["GRAFOMEM_SIGNING_KEY"] = raw_seed.hex()
    os.environ["PROVIDER_ENCRYPTION_KEY"] = Fernet.generate_key().decode('utf-8')
    
    encryption = EnvIdentity()
    registry = LLMRegistry(db_url=os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:grafomem_dev@localhost:5432/grafomem"), encryption=encryption)
    
    # 1. Register a provider with an EMPTY/MISSING key (None)
    tenant_id = "t_test_missing_key"
    registry.register_provider(
        tenant_id=tenant_id,
        provider=LLMProvider.OPENAI,
        model_id="gpt-4o",
        api_key=None
    )
    
    # 2. Trigger inference
    request = LLMRequest(
        model_id="gpt-4o",
        system_prompt="You are a helpful assistant.",
        messages=[{"role": "user", "content": "Hello!"}],
        max_tokens=10,
    )
    
    try:
        response = registry.infer(tenant_id, request)
        print("FAIL: Successfully ran inference with missing key! (fell back to platform key!)")
    except ValueError as e:
        if "API key is required" in str(e):
            print(f"SUCCESS (Fail-Closed Missing Key): {type(e).__name__}: {e}")
        else:
            print(f"FAILED (Unexpected ValueError): {e}")
    except Exception as e:
        print(f"FAILED (Wrong error type): {type(e).__name__}: {e}")

if __name__ == "__main__":
    main()
