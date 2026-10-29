# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Health and readiness probe system for local SDK and production shared services."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from nexura_runtime.service.storage_backend import DatabaseProbe, SQLiteProbe

HealthState = Literal["healthy", "unhealthy", "degraded"]


@dataclass(frozen=True)
class ProbeResult:
    """Result of an individual subsystem health probe."""

    name: str
    state: HealthState
    latency_ms: float
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class SystemHealth:
    """Aggregated system health snapshot."""

    state: HealthState
    timestamp: float
    probes: list[ProbeResult]

    @property
    def is_healthy(self) -> bool:
        return self.state == "healthy"


class HealthChecker:
    """Performs deep health and readiness verification across runtime subsystems."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        artifact_dir: str | Path | None = None,
        database: DatabaseProbe | None = None,
    ) -> None:
        self.db_path = Path(db_path).resolve() if db_path else None
        self.artifact_dir = Path(artifact_dir).resolve() if artifact_dir else None
        self.database = database or (SQLiteProbe(self.db_path) if self.db_path else None)

    def check_liveness(self) -> ProbeResult:
        """Fast liveness check: verifies process responsiveness and memory status."""
        t0 = time.perf_counter()
        details: dict[str, Any] = {
            "pid": os.getpid(),
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
        }
        duration = (time.perf_counter() - t0) * 1000.0
        return ProbeResult(
            name="process_liveness",
            state="healthy",
            latency_ms=duration,
            details=details,
        )

    def check_database(self) -> ProbeResult:
        """Read-only readiness check through the selected backend."""
        if self.database is None:
            return ProbeResult(
                name="database",
                state="healthy",
                latency_ms=0.0,
                details={"configured": False, "note": "In-memory or unconfigured database"},
            )

        t0 = time.perf_counter()
        try:
            if not self.database.ping():
                raise RuntimeError("Database ping query returned unexpected result")

            duration = (time.perf_counter() - t0) * 1000.0
            return ProbeResult(
                name="database",
                state="healthy",
                latency_ms=duration,
                details={"db_path": str(self.db_path), "status": "read_verified"},
            )
        except Exception as exc:
            duration = (time.perf_counter() - t0) * 1000.0
            return ProbeResult(
                name="database",
                state="unhealthy",
                latency_ms=duration,
                error=str(exc),
                details={"db_path": str(self.db_path)},
            )

    def check_artifact_storage(self) -> ProbeResult:
        """Readiness check: verifies artifact storage directory permissions and writeability."""
        if not self.artifact_dir:
            return ProbeResult(
                name="artifact_storage",
                state="healthy",
                latency_ms=0.0,
                details={"configured": False, "note": "Artifact storage unconfigured"},
            )

        t0 = time.perf_counter()
        try:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            probe_file = self.artifact_dir / f".health_probe_{os.getpid()}_{int(time.time())}.tmp"
            probe_file.write_text("health_check_ok", encoding="utf-8")
            content = probe_file.read_text(encoding="utf-8")
            probe_file.unlink(missing_ok=True)

            if content != "health_check_ok":
                raise RuntimeError("Artifact storage read-back failed verification")

            duration = (time.perf_counter() - t0) * 1000.0
            return ProbeResult(
                name="artifact_storage",
                state="healthy",
                latency_ms=duration,
                details={"artifact_dir": str(self.artifact_dir), "status": "read_write_verified"},
            )
        except Exception as exc:
            duration = (time.perf_counter() - t0) * 1000.0
            return ProbeResult(
                name="artifact_storage",
                state="unhealthy",
                latency_ms=duration,
                error=str(exc),
                details={"artifact_dir": str(self.artifact_dir)},
            )

    def assess_readiness(self) -> SystemHealth:
        """Aggregate all subsystem probes into a single readiness verdict."""
        probes = [
            self.check_liveness(),
            self.check_database(),
            self.check_artifact_storage(),
        ]
        overall: HealthState = "healthy"
        for probe in probes:
            if probe.state == "unhealthy":
                overall = "unhealthy"
                break
            elif probe.state == "degraded" and overall != "unhealthy":
                overall = "degraded"

        return SystemHealth(
            state=overall,
            timestamp=time.time(),
            probes=probes,
        )


def create_health_router(checker: HealthChecker | None = None) -> Any:
    """Create a FastAPI APIRouter providing /healthz and /readyz endpoints."""
    from fastapi import APIRouter, status
    from fastapi.responses import JSONResponse

    active_checker = checker or HealthChecker()
    router = APIRouter(tags=["health"])

    @router.get("/healthz")
    async def liveness() -> dict[str, Any]:
        probe = active_checker.check_liveness()
        return {
            "status": probe.state,
            "latency_ms": probe.latency_ms,
            "details": probe.details,
        }

    @router.get("/readyz")
    async def readiness() -> Any:
        import asyncio

        # The database probe may wait on the network; keep the event loop free.
        health = await asyncio.to_thread(active_checker.assess_readiness)
        body = {
            "status": health.state,
            "timestamp": health.timestamp,
            "probes": [
                {
                    "name": p.name,
                    "state": p.state,
                    "latency_ms": p.latency_ms,
                    "details": p.details,
                    "error": p.error,
                }
                for p in health.probes
            ],
        }
        return JSONResponse(
            content=body,
            status_code=status.HTTP_200_OK
            if health.is_healthy
            else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return router
