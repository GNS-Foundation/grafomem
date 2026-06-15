import os
from google import genai

# Set valid env key
os.environ["GEMINI_API_KEY"] = "VALID_ENV_KEY_MOCK"

# Try with explicitly invalid key
try:
    client = genai.Client(api_key="sk-invalid-key")
    print("Client initialized. API key used:", client.api_key)
except Exception as e:
    print("Error:", e)
