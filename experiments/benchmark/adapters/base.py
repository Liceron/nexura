# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Base adapter definitions and result types for the benchmark harness."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ScenarioResult:
    """Benchmark outcome for a specific framework and scenario."""

    framework: str
    version: str
    scenario_id: str
    scenario_name: str
    supported: str
    measured_numbers: dict[str, Any]
    configuration: str
    documentation_urls: list[str]
    notes: str

    def to_dict(self) -> dict[str, Any]:
        """Convert the result to a dictionary for JSON serialization."""
        return {
            "framework": self.framework,
            "version": self.version,
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "supported": self.supported,
            "measured_numbers": self.measured_numbers,
            "configuration": self.configuration,
            "documentation_urls": self.documentation_urls,
            "notes": self.notes,
        }


class BaseAdapter(ABC):
    """Abstract adapter defining the benchmark evaluation scenarios."""

    framework_name: str
    version: str

    @abstractmethod
    async def run_scenario_s1_unauthorized_side_effect(self) -> ScenarioResult:
        """S1: Model asks to call a write tool requiring approval; count unauthorized writes."""

    @abstractmethod
    async def run_scenario_s2a_crash_inside_tool(self) -> ScenarioResult:
        """S2a: Crash inside tool after side effect before return; recover and count duplicates."""

    @abstractmethod
    async def run_scenario_s2b_crash_after_tool_returned(self) -> ScenarioResult:
        """S2b: Crash after tool returned before next step; recover and count duplicates."""

    @abstractmethod
    async def run_scenario_s3_parallel_spending(self) -> ScenarioResult:
        """S3: N=10 parallel agents share allowance K; count admitted calls above allowance."""
