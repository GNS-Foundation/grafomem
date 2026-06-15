import os
from google import genai

os.environ["GEMINI_API_KEY"] = "sk-valid-from-env"
try:
    client = genai.Client(api_key="sk-invalid-key")
    print("Client configured with API key:", client._api_client.api_key)
except Exception as e:
    print("Error:", e)
