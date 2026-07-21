import os, json; d = {k: v for k, v in os.environ.items() if "URL" in k or "POSTGRES" in k or "PG" in k or "DATABASE" in k}; open("test_out.json", "w").write(json.dumps(d))
