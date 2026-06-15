import os
import openai

os.environ["OPENAI_API_KEY"] = "sk-valid-env-key-that-would-work"

client = openai.OpenAI(api_key="sk-invalid-user-key-12345")
print("SDK configured key:", client.api_key)
# We won't make a network request, but we can see the key
