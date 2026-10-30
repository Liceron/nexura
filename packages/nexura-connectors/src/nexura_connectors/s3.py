# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""S3-compatible object storage connector with a built-in SigV4 signer over httpx.

boto3 is not in uv.lock, so requests are signed here (AWS Signature Version 4) and sent through
the bounded, SSRF-guarded HTTP helper. Works with AWS S3, MinIO, R2 and similar services using
path-style addressing. Credential is ``access_key_id:secret_access_key``.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import re
import xml.etree.ElementTree as ET
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

from pydantic import Field

from nexura_connectors._core import (
    Call,
    ConnectorModule,
    Op,
    StrictParams,
    clamp,
    pick_cursor,
    result_data,
)
from nexura_connectors.errors import (
    InvalidConfigError,
    InvalidParamsError,
    MissingCredentialError,
    UpstreamError,
)

MAX_UPLOAD_BYTES = 5_000_000
_BUCKET = r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(
    method: str,
    url: str,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    payload: bytes = b"",
    now: dt.datetime | None = None,
    include_content_sha: bool = True,
    query: str | None = None,
) -> dict[str, str]:
    """Return the headers (host, x-amz-date, Authorization, ...) that sign this request."""
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    parts = urlsplit(url)
    host = parts.netloc
    headers = {"host": host, "x-amz-date": amz_date}
    payload_hash = hashlib.sha256(payload).hexdigest()
    if include_content_sha:
        headers["x-amz-content-sha256"] = payload_hash
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_query = query if query is not None else parts.query
    canonical = "\n".join(
        [method, parts.path or "/", canonical_query, canonical_headers, signed, payload_hash]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = _hmac(
        _hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), day), region), service), "aws4_request"
    )
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return headers


class ListObjects(StrictParams):
    """Parameters for ListObjectsV2."""

    bucket: str = Field(pattern=_BUCKET)
    prefix: str = Field(default="", max_length=1024)
    max_keys: int = Field(default=100, ge=1, le=1000)
    continuation_token: str | None = Field(default=None, max_length=1024)  # alias of ``cursor``
    cursor: str | None = Field(default=None, max_length=1024)


class ObjectRef(StrictParams):
    """Parameters addressing one object."""

    bucket: str = Field(pattern=_BUCKET)
    key: str = Field(min_length=1, max_length=1024)


class PutObject(ObjectRef):
    """Parameters for uploading an object (text or base64, at most 5 MB)."""

    text: str | None = None
    content_base64: str | None = None
    content_type: str = Field(default="application/octet-stream", max_length=100)


def _object_path(bucket: str, key: str) -> str:
    if ".." in key.split("/"):
        raise InvalidParamsError("object key must not contain '..' segments")
    return f"/{bucket}/{quote(key, safe='/')}"


def _canonical_query(params: dict[str, str]) -> str:
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params.items()))


class S3Connector(ConnectorModule):
    """List, read, write and delete objects in an S3-compatible store."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.s3"
    TITLE: ClassVar[str] = "S3-compatible storage"
    DESCRIPTION: ClassVar[str] = (
        "S3-compatible object storage with SigV4. Credential 'access_key_id:secret_access_key'; "
        "config 'region' and optional 'base_url' (MinIO, R2)."
    )
    CREDENTIAL_TYPE: ClassVar[str] = "s3_access_key"
    # SigV4 signs the request headers; PUT and DELETE of an object are idempotent anyway.
    SENDS_IDEMPOTENCY_KEY: ClassVar[bool] = False
    OPS: ClassVar[dict[str, Op]] = {
        "list_objects": Op("list_objects", False, ListObjects),
        "get_object": Op("get_object", False, ObjectRef),
        "put_object": Op("put_object", True, PutObject),
        "delete_object": Op("delete_object", True, ObjectRef),
    }

    def _prepare(self, call: Call) -> tuple[str, str, str, str]:
        access, sep, secret = call.secret.partition(":")
        if not sep or not access or not secret:
            raise MissingCredentialError("credential must be 'access_key_id:secret_access_key'")
        region = str(call.cfg.get("region") or "us-east-1")
        if not re.fullmatch(r"[a-z0-9-]{1,32}", region):
            raise InvalidConfigError("config 'region' is not a valid region name")
        override = call.cfg.get("base_url")
        if override:
            call.http.check_base_url(str(override))
        base = override or f"https://s3.{region}.amazonaws.com"
        call.http.base_origin = str(base)
        return access, secret, region, str(base).rstrip("/")

    async def _send(
        self,
        call: Call,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        write: bool = False,
    ) -> Any:
        access, secret, region, base = self._prepare(call)
        canonical_query = _canonical_query(query or {})
        url = base + path + (f"?{canonical_query}" if canonical_query else "")
        signed = sigv4_headers(
            method,
            url,
            access_key=access,
            secret_key=secret,
            region=region,
            payload=body,
            query=canonical_query,
        )
        merged = {**(headers or {}), **{k: v for k, v in signed.items() if k != "host"}}
        return await call.http.request(
            method, url, headers=merged, content=body or None, write=write
        )

    async def run(self, op: Op, params: Any, call: Call) -> dict[str, Any]:
        """Dispatch to the operation implementation."""
        if op.name == "list_objects":
            return await self._list(params, call)
        path = _object_path(params.bucket, params.key)
        if op.name == "get_object":
            resp = await self._send(call, "GET", path)
            return result_data(
                {
                    "size_bytes": len(resp.body),
                    "etag": resp.headers.get("etag"),
                    "content_base64": base64.b64encode(resp.body).decode(),
                }
            )
        if op.name == "put_object":
            if (params.text is None) == (params.content_base64 is None):
                raise InvalidParamsError("provide exactly one of text or content_base64")
            try:
                data = (
                    params.text.encode()
                    if params.text is not None
                    else base64.b64decode(params.content_base64 or "", validate=True)
                )
            except ValueError:
                raise InvalidParamsError("content_base64 is not valid base64") from None
            limit = min(int(call.cfg.get("max_upload_bytes") or MAX_UPLOAD_BYTES), MAX_UPLOAD_BYTES)
            if len(data) > limit:
                raise InvalidParamsError(f"object larger than the {limit} byte upload cap")
            resp = await self._send(
                call,
                "PUT",
                path,
                body=data,
                headers={"Content-Type": params.content_type},
                write=True,
            )
            return result_data({"etag": resp.headers.get("etag"), "size_bytes": len(data)})
        resp = await self._send(call, "DELETE", path, write=True)
        return result_data({"deleted": True, "status_code": resp.status})

    async def _list(self, p: ListObjects, call: Call) -> dict[str, Any]:
        objects: list[dict[str, Any]] = []
        token = pick_cursor(p.cursor, p.continuation_token, "continuation_token")
        for _ in range(call.max_pages):
            query = {
                "list-type": "2",
                "max-keys": str(clamp(p.max_keys, 1, 1000)),
                "prefix": p.prefix,
            }
            if token:
                query["continuation-token"] = token
            resp = await self._send(call, "GET", f"/{p.bucket}", query=query)
            try:
                root = ET.fromstring(resp.body)
            except ET.ParseError:
                raise UpstreamError("upstream returned invalid XML") from None
            uri = root.tag.partition("}")[0].lstrip("{") if root.tag.startswith("{") else ""
            ns = {"s": uri} if uri else {}
            tag = (lambda n: f"s:{n}") if uri else (lambda n: n)
            for item in root.findall(tag("Contents"), ns):
                objects.append(
                    {
                        "key": item.findtext(tag("Key"), default="", namespaces=ns),
                        "size": int(item.findtext(tag("Size"), default="0", namespaces=ns)),
                        "etag": item.findtext(tag("ETag"), default="", namespaces=ns),
                    }
                )
            truncated = root.findtext(tag("IsTruncated"), default="false", namespaces=ns)
            token = root.findtext(tag("NextContinuationToken"), namespaces=ns)
            if truncated.lower() != "true" or not token:
                token = None
                break
        return result_data(objects, next_cursor=token, truncated=bool(token))
