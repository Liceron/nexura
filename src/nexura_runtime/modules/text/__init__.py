# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Text processing, prompt templates, and safe string transformations."""

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.modules.text.errors import (
    TemplateError,
    TemplateExpansionLimitError,
    TemplateSecurityError,
    TemplateSyntaxError,
    TemplateVariableMissingError,
)
from nexura_runtime.modules.text.module import PromptTemplateModule
from nexura_runtime.modules.text.template import PromptTemplate

# Register with default module registry
default_registry.register(PromptTemplateModule())

__all__ = [
    "PromptTemplate",
    "PromptTemplateModule",
    "TemplateError",
    "TemplateVariableMissingError",
    "TemplateExpansionLimitError",
    "TemplateSyntaxError",
    "TemplateSecurityError",
]
