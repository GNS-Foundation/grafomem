import requests, os

api_url = "https://grafomem-production.up.railway.app"
headers = {"Authorization": "Bearer 580a95e4b68f485b88d86cb21f52569a"}

wf_loop = "7bee496ac5ce953728476b316ba53cf1"

print(f"Running workflow {wf_loop} to see exact SSE output...")
with requests.post(f"{api_url}/v1/orchestrator/workflows/{wf_loop}/run", headers=headers, json={"input_text": "start"}, stream=True) as r:
    for line in r.iter_lines():
        if line:
            print(line.decode())
