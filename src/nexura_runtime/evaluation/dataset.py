# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Dataset storage, versioning, serialization, and synthetic benchmark fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nexura_runtime.evaluation.errors import DatasetNotFoundError, DatasetValidationError
from nexura_runtime.evaluation.models import DatasetExample, EvaluationDataset


class DatasetRegistry:
    """In-memory and file-backed repository for versioned evaluation datasets."""

    def __init__(self, storage_dir: Path | str | None = None) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir is not None else None
        self._datasets: dict[tuple[str, str], EvaluationDataset] = {}

    def register(self, dataset: EvaluationDataset) -> None:
        """Register a versioned dataset in the registry."""
        key = (dataset.dataset_id, dataset.version)
        self._datasets[key] = dataset

    def save(self, dataset: EvaluationDataset) -> Path:
        """Save dataset to storage directory or raise error if storage_dir not configured."""
        self.register(dataset)
        if self.storage_dir is None:
            raise ValueError("Cannot save dataset: storage_dir is not configured.")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{dataset.dataset_id}_{dataset.version}.json"
        dest = self.storage_dir / filename
        dest.write_text(dataset.model_dump_json(indent=2), encoding="utf-8")
        return dest

    def load(self, dataset_id: str, version: str | None = None) -> EvaluationDataset:
        """Load dataset from in-memory cache or storage directory."""
        try:
            return self.get(dataset_id, version)
        except DatasetNotFoundError:
            if self.storage_dir is not None:
                pattern = f"{dataset_id}_{version}.json" if version else f"{dataset_id}_*.json"
                matches = list(self.storage_dir.glob(pattern))
                if matches:
                    ds = self.load_from_json(matches[0])
                    self.register(ds)
                    return ds
            raise

    def get(self, dataset_id: str, version: str | None = None) -> EvaluationDataset:
        """Retrieve dataset by ID and optional version (defaults to latest registered)."""
        if version is not None:
            key = (dataset_id, version)
            if key not in self._datasets:
                raise DatasetNotFoundError(dataset_id, version)
            return self._datasets[key]

        # Find latest by sorting versions
        matches = [ds for (d_id, _), ds in self._datasets.items() if d_id == dataset_id]
        if not matches:
            raise DatasetNotFoundError(dataset_id)
        # Sort by creation time descending
        matches.sort(key=lambda d: d.created_at, reverse=True)
        return matches[0]

    def list_datasets(self) -> list[dict[str, Any]]:
        """List summary of all datasets registered."""
        return [
            {
                "dataset_id": ds.dataset_id,
                "version": ds.version,
                "description": ds.description,
                "example_count": len(ds.examples),
                "content_hash": ds.content_hash,
                "created_at": ds.created_at,
            }
            for ds in self._datasets.values()
        ]

    @classmethod
    def load_from_json(cls, path_or_content: str | Path) -> EvaluationDataset:
        """Load an EvaluationDataset from a JSON file or raw JSON string."""
        if isinstance(path_or_content, Path) or (
            isinstance(path_or_content, str) and not path_or_content.strip().startswith("{")
        ):
            with open(path_or_content, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.loads(str(path_or_content))

        if not isinstance(data, dict):
            raise DatasetValidationError("Dataset JSON must be a top-level dictionary.")

        examples = [
            DatasetExample(
                example_id=ex.get("example_id", f"ex_{i}"),
                input_data=ex.get("input_data", {}),
                expected_output=ex.get("expected_output"),
                expected_citations=ex.get("expected_citations"),
                expected_trajectory=ex.get("expected_trajectory"),
                simulation=ex.get("simulation"),
                tags=ex.get("tags", []),
                metadata=ex.get("metadata", {}),
            )
            for i, ex in enumerate(data.get("examples", []))
        ]

        return EvaluationDataset(
            dataset_id=data.get("dataset_id", "unnamed_dataset"),
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            examples=examples,
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def save_to_json(cls, dataset: EvaluationDataset, target_path: str | Path) -> None:
        """Serialize an EvaluationDataset to disk formatted as JSON."""
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(dataset.model_dump(mode="json"), f, indent=2)


def create_synthetic_support_dataset() -> EvaluationDataset:
    """Build a standard 3-case synthetic support and retrieval evaluation dataset."""
    examples = [
        DatasetExample(
            example_id="case_01_refund_policy",
            input_data={"query": "What is the return and refund policy window for standard items?"},
            expected_output="30 days from purchase date with valid proof of purchase",
            expected_citations=["doc_refund_policy_v2#chunk_1"],
            tags=["support", "billing", "refund"],
            metadata={
                "required_substrings": ["30 days", "proof of purchase"],
                "category": "support_policy",
            },
        ),
        DatasetExample(
            example_id="case_02_subscription_cancellation",
            input_data={
                "query": "How can I cancel my enterprise plan during the cooling-off period?"
            },
            expected_output=(
                "Contact support within 14 days or submit cancellation in billing portal"
            ),
            expected_citations=["doc_terms_of_service#chunk_4"],
            tags=["support", "cancellation"],
            metadata={
                "required_substrings": ["14 days", "billing portal"],
                "category": "cancellation_policy",
            },
        ),
        DatasetExample(
            example_id="case_03_api_authentication_error",
            input_data={
                "query": "I am receiving HTTP 401 Unauthorized when querying the API endpoint."
            },
            expected_output=(
                "Verify your Bearer token is valid and passed in Authorization header"
            ),
            expected_citations=["doc_api_reference#chunk_2"],
            tags=["technical", "api", "auth"],
            metadata={
                "required_substrings": ["Bearer token", "Authorization header"],
                "category": "technical_support",
            },
        ),
    ]

    return EvaluationDataset(
        dataset_id="benchmark_customer_support",
        version="1.0.0",
        description=(
            "Synthetic support and retrieval test suite with gold citations and policy bounds."
        ),
        examples=examples,
        metadata={"domain": "customer_support", "tier": "standard"},
    )
