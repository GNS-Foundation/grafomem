import httpx
import threading
import sys

def make_req(client):
    try:
        r = client.post("/v1/governance/policies", json={"id": "test", "name": "test", "rules": []})
        print(f"Status: {r.status_code}")
        if r.status_code == 500:
            print("Got 500!")
    except Exception as e:
        print(f"Error: {e}")

def main():
    base = "http://localhost:8080"
    
    # Needs a valid token, but maybe it will 500 before checking auth if it hits the DB?
    # No, it checks auth first. But wait, I can just use UNSAFE_LOCAL_DEV and mock auth!
    client = httpx.Client(base_url=base)
    
    # We need to authenticate.
    r = client.post("/v1/portal/signup", json={"email": "test@test.com", "password": "password"})
    if r.status_code != 201:
        print("Signup failed:", r.status_code)
        return
    token = r.json()["access_token"]
    
    client.headers.update({"Authorization": f"Bearer {token}"})
    
    threads = []
    for _ in range(10):
        t = threading.Thread(target=make_req, args=(client,))
        threads.append(t)
    
    for t in threads:
        t.start()
    for t in threads:
        t.join()

if __name__ == "__main__":
    main()
