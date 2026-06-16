import re

with open("tests/test_integration_seams.py", "r") as f:
    content = f.read()

# Add imports
content = content.replace("import httpx", "from fastapi.testclient import TestClient\nfrom aml.server.app import create_app")

# Replace base_url fixture with app and client fixtures
fixtures = """@pytest.fixture(scope="module")
def app_instance(db_url):
    os.environ["GRAFOMEM_DB_URL"] = db_url
    os.environ["AUTH_MODE"] = "cloud"
    app = create_app()
    return app

@pytest.fixture(scope="module")
def client(app_instance):
    with TestClient(app_instance) as c:
        yield c
"""
content = re.sub(r'@pytest\.fixture\(scope="module"\)\ndef base_url\(\):\n    return "http://localhost:8642"', fixtures, content)

# Replace base_url args
content = content.replace("base_url", "client")

# Replace httpx calls
content = re.sub(r'httpx\.(get|post|put|delete|patch)\(\s*f"\{client\}(/[^"]*)",', r'client.\1("\2",', content)

# Replace the "with httpx.Client" block
content = re.sub(r'with httpx\.Client.*?as client:.*?response = client\.post\(\s*f"\{client\}(/[^"]*)",', r'response = client.post("\1",', content, flags=re.DOTALL)

with open("tests/test_integration_seams.py", "w") as f:
    f.write(content)

