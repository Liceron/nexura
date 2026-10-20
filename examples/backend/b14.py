"""B14: Speech Input and Output Standalone Example.

Demonstrates:
1. Keyless SpeechService initialized with deterministic httpx.MockTransport.
2. Synthesizing Hindi text into speech and persisting audio as an ArtifactReference.
3. Transcribing synthetic RIFF/WAVE audio into Unicode transcript with word timestamps.
4. Eager pre-flight validation (malformed WAV header and oversized payloads).
5. User consent enforcement for voice processing and reproduction.
6. Bounded streaming synthesis chunks with cancellation support.
7. Documented production credentialed smoke invocation.
8. Execution via B01 Module Registry and CLI.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path

import httpx
from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.artifacts.store import LocalArtifactStore
from nexura_runtime.integrations.speech.errors import (
    AudioConsentError,
    AudioValidationError,
)
from nexura_runtime.integrations.speech.module import SpeechModule
from nexura_runtime.integrations.speech.service import SpeechService
from nexura_runtime.integrations.speech.types import (
    SynthesisRequest,
    TranscriptionRequest,
    create_synthetic_wav,
)
from nexura_runtime.modules.registry import default_registry


def create_speech_demo_mock_transport() -> httpx.MockTransport:
    """Deterministic mock transport handling STT and TTS."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/speech-to-text" in path:
            return httpx.Response(
                200,
                json={
                    "transcript": "नमस्ते, नेक्सुरा में आपका स्वागत है।",
                    "language_code": "hi-IN",
                    "duration_seconds": 1.8,
                    "timestamps": [
                        {"word": "नमस्ते,", "start": 0.0, "end": 0.5},
                        {"word": "नेक्सुरा", "start": 0.55, "end": 1.0},
                        {"word": "में", "start": 1.05, "end": 1.2},
                        {"word": "आपका", "start": 1.25, "end": 1.45},
                        {"word": "स्वागत", "start": 1.5, "end": 1.7},
                        {"word": "है।", "start": 1.72, "end": 1.8},
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 10, "total_tokens": 22},
                },
            )

        # /text-to-speech
        wav_bytes = create_synthetic_wav(sample_rate=16000, duration_seconds=0.1)
        b64_str = base64.b64encode(wav_bytes).decode("ascii")
        return httpx.Response(
            200,
            json={
                "audios": [b64_str],
                "duration_seconds": 0.1,
                "usage": {"prompt_tokens": 8, "completion_tokens": 16, "total_tokens": 24},
            },
        )

    return httpx.MockTransport(handler)


async def run_b14_demo() -> None:
    print("=== B14: Speech Input and Output Demonstration ===\n")

    # 1. Keyless Service Initialization
    print("1. Initializing SpeechService with MockTransport (Keyless)...")
    mock_transport = create_speech_demo_mock_transport()
    speech_service = SpeechService(transport=mock_transport)
    print(f"  Service initialized: {speech_service}")

    # 2. Setup ExecutionContext with LocalArtifactStore
    with tempfile.TemporaryDirectory() as tmp_dir:
        artifact_store = LocalArtifactStore(root_dir=Path(tmp_dir))
        ctx = ExecutionContext(
            run_id="run_b14_demo",
            node_id="node_speech",
            artifact_store=artifact_store.for_workspace("default"),
        )

        # 3. Speech Synthesis with Artifact Persistence
        print("\n2. Executing Speech Synthesis (TTS) & Artifact Storage...")
        synth_text = "नमस्ते, नेक्सुरा में आपका स्वागत है।"
        synth_req = SynthesisRequest(
            text=synth_text,
            target_language_code="hi-IN",
            speaker="meera",
            speech_sample_rate=16000,
            user_consent=True,
            store_as_artifact=True,
        )
        synth_res = await speech_service.synthesize(synth_req, context=ctx)

        print(f"  Synthesized Audio Bytes: {len(synth_res.audio_bytes)} bytes")
        print(f"  MIME Type: {synth_res.mime_type}")
        print(f"  Sample Rate: {synth_res.sample_rate} Hz")
        if synth_res.artifact_ref:
            print(f"  Saved Artifact ID: {synth_res.artifact_ref.artifact_id}")
            print(f"  Artifact Digest: {synth_res.artifact_ref.digest}")

            # Verify artifact was persisted and can be re-read
            stored_data = await artifact_store.get(
                ctx.workspace_id, synth_res.artifact_ref.artifact_id
            )
            assert stored_data == synth_res.audio_bytes
            print("  [SUCCESS] Audio artifact successfully persisted and verified in store.")

        # 4. Speech Transcription (STT)
        print("\n3. Executing Speech Transcription (STT)...")
        synthetic_wav = create_synthetic_wav(sample_rate=16000, duration_seconds=0.08)
        trans_req = TranscriptionRequest(
            audio_data=synthetic_wav,
            language_code="hi-IN",
            with_timestamps=True,
            user_consent=True,
        )
        trans_res = await speech_service.transcribe(trans_req, context=ctx)

        print(f"  Detected Language: {trans_res.language_code}")
        print(f"  Unicode Transcript: {trans_res.transcript}")
        print(f"  Word Timestamps ({len(trans_res.timestamps)} segments):")
        for ts in trans_res.timestamps:
            print(f"    - [{ts.start_seconds:.2f}s -> {ts.end_seconds:.2f}s]: {ts.word}")

        # 5. Pre-Flight Validation Rejections
        print("\n4. Demonstrating Pre-Flight Validation Rejection...")
        bad_wav = b"NOT_A_VALID_RIFF_WAV_HEADER_DATA"
        try:
            await speech_service.transcribe(
                TranscriptionRequest(audio_data=bad_wav, user_consent=True)
            )
        except AudioValidationError as exc:
            print(f"  Correctly rejected malformed WAV before network dispatch: {exc}")

        # 6. User Consent Enforcement
        print("\n5. Demonstrating User Consent Enforcement...")
        try:
            await speech_service.synthesize(
                SynthesisRequest(text="Consent missing", user_consent=False)
            )
        except AudioConsentError as exc:
            print(f"  Correctly blocked unconsented speech synthesis: {exc}")

        # 7. Bounded Streaming Synthesis
        print("\n6. Demonstrating Bounded Streaming Synthesis...")
        stream_chunks: list[bytes] = []
        async for chunk in speech_service.stream_synthesize(
            SynthesisRequest(text="Streaming chunk test", user_consent=True),
            chunk_size_bytes=512,
            context=ctx,
        ):
            stream_chunks.append(chunk)
            assert len(chunk) <= 512

        print(f"  Received {len(stream_chunks)} bounded stream chunks (<= 512 bytes each).")

        # 8. Document Production Credentialed Smoke Invocation
        print("\n7. Production Credentialed Smoke Invocation:")
        print("   ==============================================================")
        print("   export SARVAM_API_KEY='sarvam-...'")
        print('   python -c "')
        print("   import os, asyncio")
        print("   from nexura_runtime.integrations.speech import SpeechService, SynthesisRequest")
        print("   async def run():")
        print("       key = os.environ['SARVAM_API_KEY']")
        print("       s = SpeechService(api_key=key)")
        print(
            "       res = await s.synthesize(SynthesisRequest(text='Hello India', "
            "target_language_code='en-IN'))"
        )
        print("       print(f'Synthesized {len(res.audio_bytes)} audio bytes')")
        print("   asyncio.run(run())")
        print('   "')
        print("   ==============================================================\n")

        # 9. Execute via B01 Module Interface & CLI
        print("8. Executing SpeechModule via B01 Module Registry...")
        module = default_registry.get("nexura.speech")
        assert isinstance(module, SpeechModule)
        mod_res = await module.execute(
            ctx,
            {"operation": "transcribe", "language_code": "hi-IN"},
            {},
        )
        print(f"  Module invocation status: {mod_res.status}")
        assert mod_res.status == "success"

        print("\n9. Executing via B01 CLI...")
        from nexura_runtime.modules.cli import main as cli_main

        cli_args = [
            "run",
            "nexura.speech",
            "--input",
            json.dumps({"operation": "transcribe", "language_code": "hi-IN"}),
        ]
        orig_argv = sys.argv
        sys.argv = ["nexura-module"] + cli_args
        try:
            cli_code = cli_main()
            print(f"\nCLI Execution: {'SUCCESS' if cli_code == 0 else 'FAILURE'} (code {cli_code})")
            assert cli_code == 0
        finally:
            sys.argv = orig_argv

        print("\n=== B14 Example Completed Successfully ===")


def main() -> None:
    asyncio.run(run_b14_demo())


if __name__ == "__main__":
    main()
