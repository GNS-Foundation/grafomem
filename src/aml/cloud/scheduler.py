"""GRAFOMEM Assurance Scheduler — lightweight asyncio-based periodic runner.

Runs assurance checks on per-tenant schedules using asyncio tasks.
Integrated into app lifespan (start on startup, stop on shutdown).

Sprint 19 deliverable.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("grafomem.cloud.scheduler")


class AssuranceScheduler:
    """Lightweight asyncio scheduler for continuous assurance checks.
    
    Manages one asyncio.Task per active schedule, running assurance
    checks at the configured interval.
    """

    def __init__(self, assurance_service, *, webhook_service=None) -> None:
        self._assurance = assurance_service
        self._webhooks = webhook_service
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

    async def start(self) -> None:
        """Start the scheduler. Loads all enabled schedules and creates tasks."""
        self._running = True
        logger.info("Assurance scheduler starting")
        
        schedules = self._assurance.get_all_active_schedules()
        for s in schedules:
            self.schedule(s.schedule_id, s.tenant_id, s.interval_min)
        
        logger.info(f"Loaded {len(schedules)} active schedules")

    async def stop(self) -> None:
        """Stop all scheduled tasks gracefully."""
        self._running = False
        for task_id, task in self._tasks.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info("Assurance scheduler stopped (%d tasks cancelled)", len(self._tasks))

    def schedule(self, schedule_id: str, tenant_id: str, interval_min: int) -> None:
        """Add or update a schedule. Creates an asyncio task for it."""
        if schedule_id in self._tasks:
            self._tasks[schedule_id].cancel()
        if not self._running:
            return
        task = asyncio.create_task(
            self._run_loop(schedule_id, tenant_id, interval_min),
            name=f"assurance-{schedule_id}",
        )
        self._tasks[schedule_id] = task
        logger.info("Scheduled assurance for tenant %s every %d min (id=%s)", tenant_id, interval_min, schedule_id)

    def unschedule(self, schedule_id: str) -> None:
        """Remove a schedule."""
        task = self._tasks.pop(schedule_id, None)
        if task:
            task.cancel()

    async def _run_loop(self, schedule_id: str, tenant_id: str, interval_min: int) -> None:
        """Main loop for a single schedule."""
        interval_sec = interval_min * 60
        while self._running:
            try:
                await asyncio.sleep(interval_sec)
                if not self._running:
                    break
                # Run the check in a thread to avoid blocking the event loop
                loop = asyncio.get_running_loop()
                run = await loop.run_in_executor(
                    None,
                    lambda: self._assurance.run_check(tenant_id, schedule_id=schedule_id),
                )
                logger.info(
                    "Assurance check complete: tenant=%s schedule=%s status=%s",
                    tenant_id, schedule_id, run.status,
                )
                # Alert on drift or failure
                if run.status in ("drift", "fail") and self._webhooks:
                    try:
                        self._webhooks.dispatch(
                            tenant_id=tenant_id,
                            event_type=f"assurance.{run.status}",
                            payload={
                                "run_id": run.run_id,
                                "status": run.status,
                                "drift_events": run.drift_events or [],
                            },
                        )
                    except Exception as e:
                        logger.warning("Webhook dispatch failed: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Assurance check error (tenant=%s): %s", tenant_id, e)
                await asyncio.sleep(60)  # Back off on error

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "active_schedules": self.active_count,
            "task_names": [t.get_name() for t in self._tasks.values() if not t.done()],
        }
