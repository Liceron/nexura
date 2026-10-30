# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Google Vertex AI adapter for Gemini models, authenticated with Google Cloud credentials.

Vertex AI serves the same ``generateContent`` request and response format as the Gemini
API, so this adapter reuses the Gemini translation and changes only the endpoint, the
authentication (OAuth access tokens instead of API keys) and embeddings (``:predict``).
Moved from ``nexura_agents.providers.vertex.provider`` into the ``nexura-vertex`` package.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.errors import ProviderAuthError, ProviderRequestError, ProviderResponseError
from nexura_agents.providers.errors import CapabilityError
from nexura_agents.providers.gemini.provider import GeminiNativeProvider, _google_error_codes
from nexura_agents.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_agents.types import ModelResponse, Usage

TokenProvider = Callable[[], str | Awaitable[str]]

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _describe_credentials(credentials: object) -> str:
    """Human-readable source of Application Default Credentials (no secrets)."""
    module = type(credentials).__module__
    if module.startswith("google.auth.compute_engine"):
        email = getattr(credentials, "service_account_email", "default")
        return f"this VM's service account ({email}) from the metadata server"
    if module.startswith("google.oauth2.service_account"):
        email = getattr(credentials, "service_account_email", "unknown")
        return f"service account key for {email}"
    if module.startswith("google.oauth2.credentials"):
        return "your user login from 'gcloud auth application-default login'"
    return f"{type(credentials).__name__} credentials"


class _AdcTokenSource:
    """Application Default Credentials via google-auth (``gcloud auth application-default
    login``, a service-account key in ``GOOGLE_APPLICATION_CREDENTIALS`` or the metadata
    server). Refreshes run in a worker thread because google-auth is synchronous."""

    def __init__(self) -> None:
        try:
            import google.auth
            import google.auth.transport.requests
        except ImportError as exc:
            raise ProviderAuthError(
                "Vertex AI needs Google Cloud credentials support: "
                "pip install nexura-vertex, or pass token_provider=..."
            ) from exc
        try:
            credentials, project = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
        except google.auth.exceptions.DefaultCredentialsError as exc:
            raise ProviderAuthError(
                "No Google Cloud credentials found. Run "
                "'gcloud auth application-default login' or set GOOGLE_APPLICATION_CREDENTIALS."
            ) from exc
        self._credentials = credentials
        self._request = google.auth.transport.requests.Request()
        self.project: str | None = project
        self.quota_project: str | None = getattr(credentials, "quota_project_id", None)
        self.description = _describe_credentials(credentials)

    def _token_sync(self) -> str:
        # ``valid`` turns false a few minutes before expiry, so tokens never lapse mid-call.
        if not self._credentials.valid:
            self._credentials.refresh(self._request)  # type: ignore[no-untyped-call]
        return str(self._credentials.token)

    async def __call__(self) -> str:
        return await asyncio.to_thread(self._token_sync)


class VertexAIProvider(GeminiNativeProvider):
    """Gemini on Vertex AI (``aiplatform.googleapis.com``).

    Args:
        model: Vertex model ID, e.g. ``"gemini-2.5-flash"``.
        project: Google Cloud project ID. Defaults to the project of the credentials.
        location: ``"global"`` or a region such as ``"us-central1"``.
        token_provider: Callable returning an OAuth access token (sync or async). Defaults
            to Application Default Credentials (google-auth, a dependency of this package).
    """

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        *,
        project: str | None = None,
        location: str = "global",
        token_provider: TokenProvider | None = None,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        capabilities: ProviderCapabilities | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        credential_source = "a custom token provider"
        quota_project: str | None = None
        if token_provider is None:
            adc = _AdcTokenSource()
            token_provider = adc
            # An explicit or environment project wins over the credentials' own project,
            # which on a VM is the VM's project rather than the one you meant to bill.
            project = (
                project
                or os.environ.get("GOOGLE_CLOUD_PROJECT")
                or os.environ.get("CLOUDSDK_CORE_PROJECT")
                or adc.project
            )
            credential_source = adc.description
            quota_project = adc.quota_project
        if not project:
            raise ProviderAuthError(
                "Vertex AI needs a project: pass project=... or set a default with "
                "'gcloud config set project'."
            )
        host = "aiplatform.googleapis.com"
        if location != "global":
            host = f"{location}-{host}"
        self.project = project
        self.location = location
        self._root_url = f"https://{host}/v1/projects/{project}/locations/{location}"
        super().__init__(
            model,
            api_key=None,
            base_url=f"{self._root_url}/publishers/google",
            timeout=timeout,
            client=client,
            transport=transport,
            capabilities=capabilities,
            extra_headers=extra_headers,
        )
        self._api_key = None  # Vertex authenticates with OAuth tokens only
        self.credential_source = credential_source
        self.quota_project = quota_project
        self._token_provider = token_provider
        self._access_token: str | None = None

    async def _refresh_token(self) -> None:
        token = self._token_provider()
        if inspect.isawaitable(token):
            token = await token
        if not token:
            raise ProviderAuthError("Vertex AI token provider returned an empty token.")
        self._access_token = str(token)

    def _build_headers(self) -> dict[str, str]:
        headers = super()._build_headers()
        headers.pop("x-goog-api-key", None)
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if self.quota_project:
            # User credentials must name the project that is billed for quota.
            headers["x-goog-user-project"] = self.quota_project
        return headers

    def _handle_error_response(self, response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise ProviderAuthError(
                f"Vertex AI refused the request (HTTP {response.status_code})"
                f"{_google_error_codes(response)} for project '{self.project}' using "
                f"{self.credential_source}. Check that the Vertex AI API is enabled in that "
                "project and that this identity has the 'Vertex AI User' role. To use your "
                "own login instead, run 'gcloud auth application-default login' and "
                "'gcloud auth application-default set-quota-project <project>'."
            )
        super()._handle_error_response(response)

    async def _generate_internal(
        self, request: GenerationRequest, context: ExecutionContext | None
    ) -> ModelResponse:
        await self._refresh_token()
        return await super()._generate_internal(request, context)

    def _stream_internal(
        self, request: GenerationRequest, context: ExecutionContext | None
    ) -> AsyncIterator[StreamDelta]:
        async def _authenticated() -> AsyncIterator[StreamDelta]:
            await self._refresh_token()
            async for delta in super(VertexAIProvider, self)._stream_internal(request, context):
                yield delta

        return _authenticated()

    async def _embed_internal(
        self, texts: list[str], context: ExecutionContext | None
    ) -> EmbeddingResult:
        """Embeddings via the Vertex ``:predict`` endpoint (e.g. ``gemini-embedding-001``)."""
        await self._refresh_token()
        url = f"{self.base_url}/models/{self.model}:predict"
        payload = {"instances": [{"content": text} for text in texts]}
        try:
            response = await self._get_client().post(
                url, json=payload, headers=self._build_headers(), timeout=self.timeout
            )
        except httpx.RequestError as exc:
            raise ProviderRequestError(
                f"Network error during Vertex AI embedding: {exc}", original_error=exc
            ) from exc
        self._handle_error_response(response)
        try:
            predictions = response.json()["predictions"]
            vectors = [p["embeddings"]["values"] for p in predictions]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderResponseError(
                "Malformed embedding response from Vertex AI", original_error=exc
            ) from exc

        counts = [p["embeddings"].get("statistics", {}).get("token_count") for p in predictions]
        known = all(isinstance(c, (int, float)) for c in counts)
        total = int(sum(c for c in counts if c is not None)) if known else None
        usage = Usage(prompt_tokens=total, total_tokens=total, is_complete=known)
        return EmbeddingResult(embeddings=vectors, model=self.model, usage=usage)

    async def delete_cached_content(self, cached_content_id: str) -> bool:
        """Always raises ``CapabilityError``; explicit cache management is not implemented."""
        raise CapabilityError(
            "Explicit context-cache management is not implemented for Vertex AI yet; "
            "implicit caching still applies and is reported in usage.",
            provider_name=self.model_id,
            capability="cache_management",
        )

    def __repr__(self) -> str:
        return (
            f"VertexAIProvider(model={self.model!r}, project={self.project!r}, "
            f"location={self.location!r}, timeout={self.timeout})"
        )
