# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Content type detection from magic bytes, filenames, and structure."""

from __future__ import annotations

import json
from pathlib import Path


def detect_mime_type(data: bytes, filename: str = "") -> str:
    """Detect MIME type from magic headers, file extension, and content structure."""
    ext = Path(filename).suffix.lower() if filename else ""

    # 1. Magic bytes detection
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return "application/zip"
    if data.startswith(b"\x1f\x8b"):
        return "application/gzip"

    # 2. Text-based magic sniffing
    sample = data[:1024].lstrip()
    sample_lower = sample.lower()

    if sample_lower.startswith((b"<!doctype html", b"<html")):
        return "text/html"

    # 3. Extension hint
    if ext == ".pdf":
        return "application/pdf"
    if ext == ".zip":
        return "application/zip"
    if ext == ".json":
        return "application/json"
    if ext == ".csv":
        return "text/csv"
    if ext in (".html", ".htm"):
        return "text/html"
    if ext in (".txt", ".md", ".markdown", ".log"):
        return "text/plain"

    # 4. JSON structure test
    try:
        decoded_sample = data.decode("utf-8")
        if (decoded_sample.strip().startswith("{") and decoded_sample.strip().endswith("}")) or (
            decoded_sample.strip().startswith("[") and decoded_sample.strip().endswith("]")
        ):
            json.loads(decoded_sample)
            return "application/json"
    except Exception:
        pass

    # 5. CSV heuristic (comma or tab delimited rows)
    try:
        decoded_text = data.decode("utf-8")
        lines = [line for line in decoded_text.splitlines() if line.strip()][:5]
        if (
            len(lines) >= 2
            and all("," in line or "\t" in line for line in lines)
            and (ext == ".csv" or ("," in lines[0] and len(lines[0].split(",")) > 1))
        ):
            return "text/csv"
    except Exception:
        pass

    # 6. Plain text check
    try:
        data.decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        pass

    return "application/octet-stream"
