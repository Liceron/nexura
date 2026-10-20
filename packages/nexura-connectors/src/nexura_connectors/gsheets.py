# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Google Sheets v4 connector (OAuth access token)."""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import quote

from pydantic import Field

from nexura_connectors._core import (
    Call,
    Op,
    StrictParams,
    result_data,
    safe_id,
)
from nexura_connectors._rest import RestConnector

Cell = str | int | float | bool | None


class ReadRange(StrictParams):
    """Parameters for reading an A1 range."""

    spreadsheet_id: str = Field(min_length=1, max_length=128)
    range: str = Field(min_length=1, max_length=200)


class WriteRange(ReadRange):
    """Parameters for writing rows (at most 1000 rows)."""

    values: list[list[Cell]] = Field(min_length=1, max_length=1000)


class GoogleSheetsConnector(RestConnector):
    """Read ranges; append rows or overwrite a range."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.gsheets"
    TITLE: ClassVar[str] = "Google Sheets"
    DESCRIPTION: ClassVar[str] = "Google Sheets v4 values API with an OAuth access token."
    CREDENTIAL_TYPE: ClassVar[str] = "google_oauth_token"
    DEFAULT_BASE_URL: ClassVar[str | None] = "https://sheets.googleapis.com"
    OPS: ClassVar[dict[str, Op]] = {
        "read_range": Op("read_range", False, ReadRange),
        "append_rows": Op("append_rows", True, WriteRange),
        "update_range": Op("update_range", True, WriteRange),
    }

    @staticmethod
    def _values_path(p: ReadRange) -> str:
        return f"/v4/spreadsheets/{safe_id(p.spreadsheet_id)}/values/{quote(p.range, safe='')}"

    async def op_read_range(self, p: ReadRange, call: Call) -> dict[str, Any]:
        """Read a range; rows beyond the response cap are rejected (read)."""
        data = (await self.api(call, "GET", self._values_path(p))).json()
        return result_data(data.get("values", []))

    async def op_append_rows(self, p: WriteRange, call: Call) -> dict[str, Any]:
        """Append rows after the table in the range (write)."""
        resp = await self.api(
            call,
            "POST",
            self._values_path(p) + ":append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": p.values},
            write=True,
        )
        updates = resp.json().get("updates", {})
        return result_data(
            {
                "updated_range": updates.get("updatedRange"),
                "updated_rows": updates.get("updatedRows"),
            }
        )

    async def op_update_range(self, p: WriteRange, call: Call) -> dict[str, Any]:
        """Overwrite a range (write)."""
        resp = await self.api(
            call,
            "PUT",
            self._values_path(p),
            params={"valueInputOption": "USER_ENTERED"},
            json={"range": p.range, "values": p.values},
            write=True,
        )
        data = resp.json()
        return result_data(
            {"updated_range": data.get("updatedRange"), "updated_cells": data.get("updatedCells")}
        )
