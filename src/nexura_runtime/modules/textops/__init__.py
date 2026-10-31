# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline textops builder components and default registrations."""

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.modules.textops.case import CaseModule
from nexura_runtime.modules.textops.clean import CleanModule
from nexura_runtime.modules.textops.count import CountModule
from nexura_runtime.modules.textops.html_to_text import HtmlToTextModule
from nexura_runtime.modules.textops.join import JoinModule
from nexura_runtime.modules.textops.markdown_to_text import MarkdownToTextModule
from nexura_runtime.modules.textops.regex_extract import RegexExtractModule
from nexura_runtime.modules.textops.replace import ReplaceModule
from nexura_runtime.modules.textops.split import SplitModule
from nexura_runtime.modules.textops.truncate import TruncateModule

COMPONENT_CLASSES = (
    SplitModule,
    JoinModule,
    ReplaceModule,
    RegexExtractModule,
    CaseModule,
    CleanModule,
    TruncateModule,
    CountModule,
    HtmlToTextModule,
    MarkdownToTextModule,
)

for _component_class in COMPONENT_CLASSES:
    default_registry.register(_component_class())

__all__ = [
    "SplitModule",
    "JoinModule",
    "ReplaceModule",
    "RegexExtractModule",
    "CaseModule",
    "CleanModule",
    "TruncateModule",
    "CountModule",
    "HtmlToTextModule",
    "MarkdownToTextModule",
    "COMPONENT_CLASSES",
]
