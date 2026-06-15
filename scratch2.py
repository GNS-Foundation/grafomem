import os
import openai

os.environ["OPENAI_API_KEY"] = "sk-valid-from-env"
try:
    client = openai.OpenAI(api_key="sk-invalid-key")
    print("Client configured with:", client.api_key)
except Exception as e:
    print("Error:", e)
