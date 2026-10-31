# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Extract visible HTML text using the standard library parser."""

from html.parser import HTMLParser
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import TEXT, ComponentModule, manifest


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden.append(tag)
        elif not self.hidden and tag in {"p", "div", "br", "li", "h1", "h2", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in {"p", "div", "li", "h1", "h2", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


class HtmlToTextModule(ComponentModule):
    manifest = manifest(
        "text",
        "html_to_text",
        "Extract text and decode entities; drop script/style contents and separate blocks.",
        {"text": TEXT},
        ["text"],
        {"text": TEXT},
        {"text": "<p>Hello <b>world</b></p>"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        parser = _TextParser()
        parser.feed(inputs["text"])
        parser.close()
        return {
            "text": "\n".join(
                line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
            )
        }
