import psycopg
import os
import json
from enum import Enum

class PolicyType(str, Enum):
    rate_limit = "rate_limit"

class PolicyAction(str, Enum):
    deny = "deny"

class Policy:
    pass

db_url = os.environ.get("GRAFOMEM_DB_URL")
try:
    with psycopg.connect(db_url) as conn:
        conn.execute("SELECT %s", (Policy(),))
except Exception as e:
    print("Policy:", repr(e))

try:
    with psycopg.connect(db_url) as conn:
        conn.execute("SELECT %s", (PolicyType.rate_limit,))
except Exception as e:
    print("PolicyType:", repr(e))

