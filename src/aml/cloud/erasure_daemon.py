import os
import time
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from prometheus_client import start_http_server, Counter, Gauge

from aml.cloud.erasure_sweeper import ErasureSweeper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Prometheus Metrics
grafomem_erasure_sweeps_total = Counter(
    "grafomem_erasure_sweeps_total", 
    "Total number of sweep runs"
)
grafomem_erasure_embeddings_swept_total = Counter(
    "grafomem_erasure_embeddings_swept_total", 
    "Total number of embeddings successfully deleted"
)
grafomem_erasure_sweep_errors_total = Counter(
    "grafomem_erasure_sweep_errors_total", 
    "Total number of sweep failures"
)
grafomem_erasure_last_sweep_time_seconds = Gauge(
    "grafomem_erasure_last_sweep_time_seconds", 
    "Timestamp of the last successful sweep"
)

def run_sweep_job(db_url: str):
    logger.info("Starting scheduled sweep job...")
    try:
        # We sweep with window_minutes=60 (or an env var config)
        window = int(os.environ.get("GRAFOMEM_ERASURE_WINDOW_MINUTES", "60"))
        # Using production table prefix by default if none specified
        table_prefix = os.environ.get("GRAFOMEM_ERASURE_TABLE_PREFIX", "")
        
        sweeper = ErasureSweeper(db_url=db_url, window_minutes=window, table_prefix=table_prefix)
        swept_count = sweeper.sweep()
        
        grafomem_erasure_sweeps_total.inc()
        if swept_count > 0:
            grafomem_erasure_embeddings_swept_total.inc(swept_count)
            logger.info(f"Sweep job completed: {swept_count} embeddings swept.")
        else:
            logger.info("Sweep job completed: 0 embeddings swept.")
            
        grafomem_erasure_last_sweep_time_seconds.set_to_current_time()
        
    except Exception as e:
        logger.error(f"Sweep job failed: {e}", exc_info=True)
        grafomem_erasure_sweep_errors_total.inc()

def start_daemon(db_url: str, metrics_port: int = 9091, interval_minutes: int = 1):
    """Start the APScheduler daemon and metrics server."""
    logger.info(f"Starting Prometheus metrics server on port {metrics_port}")
    try:
        start_http_server(metrics_port)
    except OSError as e:
        logger.warning(f"Failed to start metrics server on {metrics_port} (likely running in tests): {e}")
    
    logger.info(f"Starting APScheduler, interval={interval_minutes} minutes")
    scheduler = BackgroundScheduler()
    # Trigger immediately on start, then every interval
    scheduler.add_job(
        run_sweep_job, 
        'interval', 
        minutes=interval_minutes, 
        args=[db_url], 
        id='erasure_sweeper_job',
        replace_existing=True,
        # Avoid running immediately in tests if we just want to start the daemon
        # But for production it's good to run on startup.
        # We will let the scheduler handle the next run time according to the interval.
    )
    scheduler.start()
    
    return scheduler

if __name__ == "__main__":
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is required")
        exit(1)
        
    scheduler = start_daemon(db_url)
    try:
        # Keep the main thread alive
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Erasure daemon shut down.")
