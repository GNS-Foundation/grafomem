import os
import requests

api_url = os.environ.get("GRAFOMEM_API_URL", "http://localhost:8000")
print("We need to fetch the step details for the last workflow.")
# But we don't have the workflow ID... 
# Actually, the user's log says "steps=2/6".
