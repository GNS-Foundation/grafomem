import os
import traceback
from aml.cloud.db_pool import RoutingPool
from aml.cloud.orchestrator import OrchestratorService

def main():
    db_url = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
    pool = RoutingPool(db_url)
    pool.open()
    
    orch = OrchestratorService(
        db_url,
        governance=None,
        decision_trail=None,
        pool=pool,
    )
    
    try:
        wf = orch.get_workflow("nonexistent-wf")
        print("Success! wf =", wf)
    except Exception as e:
        print("Failed!")
        traceback.print_exc()

if __name__ == "__main__":
    main()
