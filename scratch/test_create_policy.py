import sys
import os
import psycopg
sys.path.insert(0, os.path.abspath('src'))
from aml.cloud.governance import GovernanceGateway, PolicyType, PolicyAction

db_url = os.environ.get("GRAFOMEM_DB_URL")
if not db_url:
    print("No DB URL")
    sys.exit(0)

from psycopg_pool import ConnectionPool
pool = ConnectionPool(db_url)
gateway = GovernanceGateway(pool, None)

try:
    p = gateway.create_policy(
        tenant_id="test",
        name="test",
        description="test",
        policy_type=PolicyType.rate_limit,
        action=PolicyAction.deny,
        config={}
    )
    print("Success:", p.policy_id)
except Exception as e:
    print("Error:", repr(e))
