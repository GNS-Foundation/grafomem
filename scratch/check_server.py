import httpx

def main():
    try:
        r = httpx.get("http://localhost:8080/healthz")
        print("Healthz:", r.status_code, r.text)
    except Exception as e:
        print("Failed to reach server:", e)

if __name__ == "__main__":
    main()
