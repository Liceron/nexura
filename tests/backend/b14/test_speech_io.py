"""Comprehensive acceptance and unit tests for Module B14: Speech Input and Output (STT / TTS)."""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

import httpx
import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.contracts.errors import SchemaValidationError
from nexura_runtime.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
)
from nexura_runtime.providers.errors import StreamCancelledError

from nexura_runtime.artifacts import LocalArtifactStore
from nexura_runtime.integrations.speech.errors import (
    AudioConsentError,
    AudioValidationError,
    SpeechProviderError,
)
from nexura_runtime.integrations.speech.module import SpeechModule
from nexura_runtime.integrations.speech.service import SpeechService
from nexura_runtime.integrations.speech.types import (
    SynthesisRequest,
    TranscriptionRequest,
    create_synthetic_wav,
    validate_audio_bytes,
)
from nexura_runtime.modules.registry import default_registry


@pytest.fixture
def sample_wav() -> bytes:
    """Generate deterministic synthetic 16kHz WAV."""
    return create_synthetic_wav(duration_seconds=0.5, sample_rate=16000)


def test_validate_audio_bytes_success(sample_wav: bytes) -> None:
    """Acceptance case: Valid synthetic WAV passes pre-flight validation."""
    meta = validate_audio_bytes(sample_wav, max_size_bytes=100_000)
    assert meta["mime_type"] == "audio/wav"
    assert meta["sample_rate"] == 16000
    assert meta["channels"] == 1
    assert meta["size_bytes"] == len(sample_wav)


def test_validate_audio_bytes_malformed() -> None:
    """Acceptance case: Malformed/non-WAV bytes rejected before sending."""
    garbage = b"NOT_A_VALID_WAVE_HEADER_GARBAGE_PAYLOAD"
    with pytest.raises(AudioValidationError, match="Expected valid RIFF/WAVE header"):
        validate_audio_bytes(garbage)


def test_validate_audio_bytes_oversized(sample_wav: bytes) -> None:
    """Acceptance case: Audio exceeding size limit rejected before sending."""
    with pytest.raises(AudioValidationError, match="exceeds maximum limit"):
        validate_audio_bytes(sample_wav, max_size_bytes=100)


def test_validate_audio_bytes_unsupported_sample_rate() -> None:
    """Acceptance case: Audio with unsupported sample rate rejected."""
    odd_wav = create_synthetic_wav(duration_seconds=0.1, sample_rate=11025)
    with pytest.raises(AudioValidationError, match="Unsupported sample rate"):
        validate_audio_bytes(odd_wav)


@pytest.mark.asyncio
async def test_speech_synthesis_tts_and_artifact_storage() -> None:
    """Acceptance case: TTS synthesis returns audio and stores ArtifactRef."""
    synthetic_wav = create_synthetic_wav(duration_seconds=0.2, sample_rate=16000)
    b64_audio = base64.b64encode(synthetic_wav).decode("ascii")

    def mock_tts_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/text-to-speech"
        data = json.loads(request.content.decode("utf-8"))
        assert data["inputs"] == ["Hello world"]
        assert data["target_language_code"] == "en-IN"
        return httpx.Response(
            200,
            json={
                "audios": [b64_audio],
                "sample_rate": 16000,
            },
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        artifact_store = LocalArtifactStore(root_dir=Path(tmp_dir))
        transport = httpx.MockTransport(mock_tts_handler)
        service = SpeechService(
            api_key="test-key",
            transport=transport,
            artifact_store=artifact_store,
        )

        ctx = ExecutionContext(
            run_id="run-test",
            node_id="node-test",
            workspace_id="test_ws",
            artifact_store=artifact_store.for_workspace("test_ws"),
        )

        req = SynthesisRequest(
            text="Hello world",
            target_language_code="en-IN",
            user_consent=True,
            store_as_artifact=True,
        )
        result = await service.synthesize(req, context=ctx)

        assert result.audio_bytes == synthetic_wav
        assert result.mime_type == "audio/wav"
        assert result.sample_rate == 16000
        assert result.artifact_ref is not None
        assert result.artifact_ref.artifact_id.startswith("speech_tts_")

        # Verify artifact store retrieval
        stored_bytes = await artifact_store.get("test_ws", result.artifact_ref.artifact_id)
        assert stored_bytes == synthetic_wav


@pytest.mark.asyncio
async def test_speech_synthesis_consent_enforced() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    service = SpeechService(api_key="test-key", transport=transport)
    req = SynthesisRequest(
        text="Voice clone attempt",
        target_language_code="en-IN",
        user_consent=False,  # User did not grant consent
    )
    with pytest.raises(AudioConsentError, match="user consent is required"):
        await service.synthesize(req)


@pytest.mark.asyncio
async def test_speech_transcription_stt_unicode_and_timestamps(sample_wav: bytes) -> None:
    """Acceptance case: Unicode transcript and accurate timestamps preserved."""

    def mock_stt_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/speech-to-text"
        return httpx.Response(
            200,
            json={
                "transcript": "नमस्ते नेक्सुरा",
                "language_code": "hi-IN",
                "timestamps": {
                    "words": ["नमस्ते", "नेक्सुरा"],
                    "start_time_seconds": [0.0, 0.4],
                    "end_time_seconds": [0.35, 0.8],
                },
            },
        )

    transport = httpx.MockTransport(mock_stt_handler)
    service = SpeechService(api_key="test-key", transport=transport)

    req = TranscriptionRequest(
        audio_data=sample_wav,
        language_code="hi-IN",
        with_timestamps=True,
        user_consent=True,
    )
    result = await service.transcribe(req)

    assert result.language_code == "hi-IN"
    assert result.transcript == "नमस्ते नेक्सुरा"
    assert len(result.timestamps) == 2
    assert result.timestamps[0].word == "नमस्ते"
    assert result.timestamps[0].start_seconds == 0.0
    assert result.timestamps[0].end_seconds == 0.35
    assert result.timestamps[1].word == "नेक्सुरा"
    assert result.duration_seconds == 0.8


@pytest.mark.asyncio
async def test_speech_transcription_from_artifact(sample_wav: bytes) -> None:
    """Acceptance case: Transcription directly from ArtifactRef."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        artifact_store = LocalArtifactStore(root_dir=Path(tmp_dir))
        ref = await artifact_store.put("audio_ws", "input_audio", sample_wav, mime_type="audio/wav")

        def mock_stt_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"transcript": "Audio from artifact", "language_code": "en-IN"},
            )

        transport = httpx.MockTransport(mock_stt_handler)
        service = SpeechService(
            api_key="test-key",
            transport=transport,
            artifact_store=artifact_store,
        )

        ctx = ExecutionContext(
            run_id="run-stt",
            node_id="node-stt",
            workspace_id="audio_ws",
            artifact_store=artifact_store.for_workspace("audio_ws"),
        )

        req = TranscriptionRequest(
            artifact_id=ref.artifact_id,
            user_consent=True,
        )
        result = await service.transcribe(req, context=ctx)
        assert result.transcript == "Audio from artifact"


@pytest.mark.asyncio
async def test_speech_streaming_bounded_chunks() -> None:
    """Acceptance case: Mocked streaming synthesis yields bounded chunks."""
    synthetic_wav = create_synthetic_wav(duration_seconds=0.5, sample_rate=16000)
    b64_audio = base64.b64encode(synthetic_wav).decode("ascii")

    def mock_tts_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"audios": [b64_audio], "sample_rate": 16000},
        )

    transport = httpx.MockTransport(mock_tts_handler)
    service = SpeechService(api_key="test-key", transport=transport)

    req = SynthesisRequest(
        text="Stream test",
        target_language_code="en-IN",
        user_consent=True,
    )

    chunk_size = 256
    chunks: list[bytes] = []
    async for chunk in service.synthesize_stream(req, chunk_size=chunk_size):
        assert len(chunk) <= chunk_size
        chunks.append(chunk)

    reassembled = b"".join(chunks)
    assert reassembled == synthetic_wav


@pytest.mark.asyncio
async def test_speech_timeout_cancellation(sample_wav: bytes) -> None:
    """Acceptance case: Timeout or context cancellation cleanly halts request."""
    # Test context cancellation
    ctx = ExecutionContext(
        run_id="run-cancel",
        node_id="node-cancel",
        is_cancelled=True,
    )
    service = SpeechService(api_key="test-key")
    with pytest.raises(StreamCancelledError, match="cancelled"):
        await service.transcribe(
            TranscriptionRequest(audio_data=sample_wav, user_consent=True),
            context=ctx,
        )

    # Test network timeout
    def mock_timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Socket read timed out")

    timeout_service = SpeechService(
        api_key="test-key",
        transport=httpx.MockTransport(mock_timeout_handler),
    )
    with pytest.raises(SpeechProviderError, match="timed out"):
        await timeout_service.synthesize(SynthesisRequest(text="Timeout test", user_consent=True))


@pytest.mark.asyncio
async def test_speech_http_error_mappings(sample_wav: bytes) -> None:
    """Acceptance case: HTTP errors mapped to Provider*Error types."""
    # 401 Auth error
    t_401 = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "Invalid API key"}))
    service_401 = SpeechService(api_key="invalid-key", transport=t_401)
    with pytest.raises(ProviderAuthError):
        await service_401.transcribe(TranscriptionRequest(audio_data=sample_wav, user_consent=True))

    # 429 Rate limit
    t_429 = httpx.MockTransport(
        lambda r: httpx.Response(429, json={"error": "Rate limit exceeded"})
    )
    service_429 = SpeechService(api_key="key", transport=t_429)
    with pytest.raises(ProviderRateLimitError):
        await service_429.synthesize(SynthesisRequest(text="Hi", user_consent=True))

    # 400 Bad request
    service_400 = SpeechService(
        api_key="key",
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "Bad payload"})),
    )
    with pytest.raises(ProviderRequestError):
        await service_400.transcribe(TranscriptionRequest(audio_data=sample_wav, user_consent=True))

    # 500 Server error
    service_500 = SpeechService(
        api_key="key",
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="Internal Error")),
    )
    with pytest.raises(ProviderResponseError):
        await service_500.synthesize(SynthesisRequest(text="Hi", user_consent=True))


@pytest.mark.asyncio
async def test_speech_module_b00_b01_registry(sample_wav: bytes) -> None:
    """Acceptance case: SpeechModule registered and executed via Module Registry."""
    reg = default_registry
    speech_mod = reg.get("nexura.speech")
    assert speech_mod is not None
    assert isinstance(speech_mod, SpeechModule)
    assert speech_mod.manifest.module_id == "nexura.speech"

    # Execute transcribe through Module interface
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "transcript": "परीक्षण संदेश",
                "language_code": "hi-IN",
                "timestamps": {
                    "words": ["परीक्षण", "संदेश"],
                    "start_time_seconds": [0.0, 0.5],
                    "end_time_seconds": [0.4, 0.9],
                },
            },
        )

    service = SpeechService(api_key="mock", transport=httpx.MockTransport(mock_handler))
    custom_module = SpeechModule(service=service)

    ctx = ExecutionContext(run_id="run-reg", node_id="node-reg")
    b64_audio = base64.b64encode(sample_wav).decode("ascii")

    result = await custom_module.execute(
        ctx,
        {
            "operation": "transcribe",
            "audio_base64": b64_audio,
            "language_code": "hi-IN",
        },
        {},
    )

    assert result.status == "success"
    assert result.outputs["transcript"] == "परीक्षण संदेश"
    assert result.outputs["language_code"] == "hi-IN"
    assert len(result.outputs["timestamps"]) == 2


@pytest.mark.asyncio
async def test_speech_module_schema_validation() -> None:
    """Acceptance case: Invalid actions or inputs rejected by schema validator."""
    module = SpeechModule()
    ctx = ExecutionContext(run_id="run-val", node_id="node-val")

    # Missing required inputs
    with pytest.raises(SchemaValidationError):
        await module.execute(ctx, {}, {})

    # Invalid type
    with pytest.raises(SchemaValidationError):
        await module.execute(ctx, {"operation": "synthesize", "sample_rate": "invalid"}, {})

    # Unknown operation
    with pytest.raises(SchemaValidationError):
        await module.execute(ctx, {"operation": "unknown_operation"}, {})
