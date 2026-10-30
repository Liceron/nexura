# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Google Vertex AI provider for Nexura agents.

Distributed as ``nexura-vertex`` (it depends on ``nexura-agents`` and google-auth). Once it
is installed, ``"vertex:<model>"`` model strings in ``nexura_agents.infer_model`` and
``Agent(model=...)`` build ``VertexAIProvider``.
"""

from nexura_vertex.provider import TokenProvider, VertexAIProvider

__all__ = ["TokenProvider", "VertexAIProvider"]
