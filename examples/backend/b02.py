"""Standalone executable example for B02: Artifact and file storage.

Demonstrates:
- Uploading and persisting a Hindi Unicode text file into LocalArtifactStore.
- Generating a workspace-scoped ArtifactReference.
- Passing the artifact reference to ArtifactReaderModule.
- Reading and verifying content integrity keylessly without Studio.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from nexura_runtime.contracts import ExecutionContext

from nexura_runtime.artifacts import ArtifactReaderModule, LocalArtifactStore


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = LocalArtifactStore(root_dir=tmpdir)
        workspace_id = "ws_india_enterprise"
        artifact_id = "hindi_brief_2026"

        # Content in Devanagari Hindi
        hindi_content = "नमस्ते दुनिया — यह एक नेक्सुरा आर्टिफ़ैक्ट है। शुद्ध और सुरक्षित डेटा।"
        raw_bytes = hindi_content.encode("utf-8")

        print(f"1. Storing Hindi artifact in workspace '{workspace_id}'...")
        ref = await store.put(
            workspace_id=workspace_id,
            artifact_id=artifact_id,
            data=raw_bytes,
            mime_type="text/plain; charset=utf-8",
            metadata={"language": "hi", "category": "briefing"},
        )

        print(f"Artifact URI: {ref.uri}")
        print(f"Artifact Digest: {ref.digest}")
        print(f"Artifact Size: {ref.size_bytes} bytes")

        # Verify it exists and can be retrieved directly
        read_bytes = await store.get(workspace_id, artifact_id)
        assert read_bytes == raw_bytes
        print("Direct retrieval verified.")

        # 2. Invoke ArtifactReaderModule with execution context
        module = ArtifactReaderModule()
        ctx = ExecutionContext(
            workspace_id=workspace_id,
            principal_id="analyst_user",
            run_id="run_b02_artifact",
            node_id="node_read_artifact",
            artifact_store=store.for_workspace(workspace_id),
            attributes={"artifact_store": store},
        )

        result = await module.execute(
            ctx,
            inputs={"artifact_id": artifact_id},
            config={"encoding": "utf-8"},
        )

        print(f"Module execution status: {result.status}")
        print(f"Extracted content: {result.outputs['content']}")
        print(f"Extracted size: {result.outputs['size_bytes']}")

        assert result.status == "success"
        assert result.outputs["content"] == hindi_content
        assert result.outputs["size_bytes"] == len(raw_bytes)
        assert result.outputs["digest"] == ref.digest

        # Check temporary folder structure has clean workspace isolation
        ws_path = Path(tmpdir) / workspace_id
        assert (ws_path / f"{artifact_id}.bin").is_file()
        assert (ws_path / f"{artifact_id}.meta.json").is_file()
        assert list((ws_path / ".tmp").glob("*.tmp")) == []  # no leftover temporary files

        print("B02 standalone example executed and verified successfully.")


if __name__ == "__main__":
    asyncio.run(main())
