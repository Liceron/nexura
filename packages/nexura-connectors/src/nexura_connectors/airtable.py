# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Airtable REST connector (personal access token)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from nexura_connectors._core import (
    Call,
    Op,
    StrictParams,
    check_request_size,
    pick_cursor,
    result_data,
    safe_id,
)
from nexura_connectors._rest import RestConnector


class Table(StrictParams):
    """Base and table coordinates."""

    base_id: str = Field(min_length=1, max_length=64)
    table: str = Field(min_length=1, max_length=64)


class ListRecords(Table):
    """Parameters for listing records."""

    page_size: int = Field(default=100, ge=1, le=100)
    filter_by_formula: str | None = Field(default=None, max_length=1000)
    offset: str | None = None  # alias of ``cursor`` (kept for compatibility)
    cursor: str | None = None


class GetRecord(Table):
    """Parameters for fetching one record."""

    record_id: str = Field(min_length=1, max_length=64)


class CreateRecords(Table):
    """Parameters for creating up to 10 records."""

    records: list[dict[str, Any]] = Field(min_length=1, max_length=10)


class UpdateRecords(Table):
    """Parameters for patching up to 10 records; each needs 'id' and 'fields'."""

    records: list[dict[str, Any]] = Field(min_length=1, max_length=10)


class AirtableConnector(RestConnector):
    """List and fetch records; create and update records."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.airtable"
    TITLE: ClassVar[str] = "Airtable"
    DESCRIPTION: ClassVar[str] = "Airtable REST API with a personal access token."
    CREDENTIAL_TYPE: ClassVar[str] = "airtable_token"
    DEFAULT_BASE_URL: ClassVar[str | None] = "https://api.airtable.com/v0"
    OPS: ClassVar[dict[str, Op]] = {
        "list_records": Op("list_records", False, ListRecords),
        "get_record": Op("get_record", False, GetRecord),
        "create_records": Op("create_records", True, CreateRecords),
        "update_records": Op("update_records", True, UpdateRecords),
    }

    @staticmethod
    def _path(p: Table) -> str:
        return f"/{safe_id(p.base_id, 'base')}/{safe_id(p.table, 'table')}"

    async def op_list_records(self, p: ListRecords, call: Call) -> dict[str, Any]:
        """List records with offset pagination (read)."""
        records: list[Any] = []
        offset = pick_cursor(p.cursor, p.offset, "offset")
        for _ in range(call.max_pages):
            query: dict[str, Any] = {"pageSize": p.page_size}
            if p.filter_by_formula:
                query["filterByFormula"] = p.filter_by_formula
            if offset:
                query["offset"] = offset
            data = (await self.api(call, "GET", self._path(p), params=query)).json()
            records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break
        return result_data(records, next_cursor=offset, truncated=bool(offset))

    async def op_get_record(self, p: GetRecord, call: Call) -> dict[str, Any]:
        """Fetch one record (read)."""
        path = f"{self._path(p)}/{safe_id(p.record_id, 'record id')}"
        return result_data((await self.api(call, "GET", path)).json())

    async def op_create_records(self, p: CreateRecords, call: Call) -> dict[str, Any]:
        """Create records (write)."""
        check_request_size(p.records, "airtable records")
        resp = await self.api(call, "POST", self._path(p), json={"records": p.records}, write=True)
        return result_data([r.get("id") for r in resp.json().get("records", [])])

    async def op_update_records(self, p: UpdateRecords, call: Call) -> dict[str, Any]:
        """Patch records by id (write)."""
        check_request_size(p.records, "airtable records")
        resp = await self.api(call, "PATCH", self._path(p), json={"records": p.records}, write=True)
        return result_data([r.get("id") for r in resp.json().get("records", [])])
