import os
from google import genai

# Give it a real valid env key so we see if it falls back to it despite the bad constructor key
valid_key = os.environ.get("GEMINI_API_KEY")
if not valid_key:
    print("No GEMINI_API_KEY set to test with.")
    exit(0)

try:
    client = genai.Client(api_key="sk-invalid-key-12345")
    response = client.models.generate_content(
        model='gemini-2.5-pro',
        contents='Say hello.'
    )
    print("SUCCESS! Output:", response.text)
except Exception as e:
    print("FAILED:", e)
