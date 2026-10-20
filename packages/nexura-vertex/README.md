# Nexura Vertex

Google Vertex AI models for Nexura agents, using Google Application Default Credentials.

## Install

```bash
pip install nexura-vertex
```

Alpha and unpublished: install from the [source checkout](https://github.com/Liceron/nexura) or local wheels until publication. See [package installation](https://github.com/Liceron/nexura/blob/main/docs/packages.md).

## Minimal example

This example uses mock HTTP and a fixed fake token, without credentials or calls to Google. For live calls, use `Agent(model="vertex:gemini-2.5-flash")` after configuring ADC (`gcloud auth application-default login`, a service account or VM metadata). Project selection also accepts `GOOGLE_CLOUD_PROJECT` or `project=`; custom tokens use `token_provider=`. Live calls are billed to your project.

```python
import httpx
from nexura_agents import Agent, Runner

from nexura_vertex import VertexAIProvider


def fake_vertex(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer test-token"
    assert "/projects/demo/locations/global/" in request.url.path
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "Hello"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
        },
    )


model = VertexAIProvider(
    "gemini-2.5-flash",
    project="demo",
    token_provider=lambda: "test-token",
    transport=httpx.MockTransport(fake_vertex),
)
result = Runner().run(Agent(name="Hi", instructions="Greet.", model=model), "hi")
print(result.final_output)
```

## Status and limits

> Status: **alpha** (0.4). Gemini 2.5 Flash on Vertex AI passed one live check run by the
> repository owner on 2026-10-08 (a plain reply, a tool call and streaming). Everything else is
> tested offline with mock HTTP.
> Depends only on `nexura-agents` and `google-auth`, not `nexura-runtime`; Python 3.11–3.13. The adapter reuses Gemini tools/streaming/structured output/multimodal parts, with OAuth endpoints and `:predict` embeddings.

## Documentation and licence

[Main documentation](https://github.com/Liceron/nexura/blob/main/docs/README.md) · [Full guide](https://github.com/Liceron/nexura/blob/main/docs/providers.md#vertex-ai). MIT, Copyright (c) 2026 Liceron. See [LICENSE](https://github.com/Liceron/nexura/blob/main/packages/nexura-vertex/LICENSE).
