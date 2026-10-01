"""Immich API client: duplicate checks now, uploads later.

The API key is read from a file and only ever placed in the ``x-api-key`` request header. It is
never logged, printed or included in exception messages.
"""

import hashlib
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from googich_takeaway.takeout.archives import Readable

CHECK_BATCH = 1000
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
DEVICE_ID = "googich-takeaway"
UPLOAD_CHUNK = 1024 * 1024


class ImmichError(Exception):
    """Immich could not be reached or answered with an error."""


class CheckAction(StrEnum):
    ACCEPT = "accept"
    """Immich does not have this file; it would be uploaded."""
    REJECT = "reject"


@dataclass(frozen=True)
class CheckResult:
    action: CheckAction
    reason: str | None
    """For rejections: ``duplicate``, ``unsupported-format`` and so on."""
    existing_asset: str | None
    is_trashed: bool


class UploadIntegrityError(ImmichError):
    """The bytes sent did not match the file that was scanned."""


@dataclass(frozen=True)
class UploadResult:
    asset_id: str
    duplicate: bool
    """Immich already had the file; nothing new was stored."""


@dataclass(frozen=True)
class AssetDates:
    date_time_original: datetime | None
    """Capture instant Immich stored, timezone-aware."""
    local_date_time: datetime | None
    """Wall-clock time Immich displays, naive."""
    time_zone: str | None


def read_api_key(path: Path) -> str:
    """Read an API key file, refusing files other users can read."""
    mode = path.stat().st_mode
    if mode & 0o077:
        raise ImmichError(f"{path} is readable by other users; run: chmod 600 {path}")
    key = path.read_text().strip()
    if not key:
        raise ImmichError(f"{path} is empty")
    return key


class ImmichClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api",
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=TIMEOUT,
            transport=transport,
        )

    def __enter__(self) -> "ImmichClient":
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def server_version(self) -> str:
        data = self._request("GET", "/server/version")
        return f"{data['major']}.{data['minor']}.{data['patch']}"

    def key_permissions(self) -> list[str]:
        """Permissions of the API key in use. Works with any key, whatever its permissions."""
        data = self._request("GET", "/api-keys/me")
        permissions = data.get("permissions")
        return [str(p) for p in permissions] if isinstance(permissions, list) else []

    def check_existing(self, checksums: Iterable[tuple[str, str]]) -> dict[str, CheckResult]:
        """Ask Immich which files it already has.

        ``checksums`` are ``(id, sha1_hex)`` pairs; the id is any string unique within the call.
        Read-only: nothing is created on the server.
        """
        pending = list(checksums)
        results: dict[str, CheckResult] = {}
        for start in range(0, len(pending), CHECK_BATCH):
            batch = pending[start : start + CHECK_BATCH]
            data = self._request(
                "POST",
                "/assets/bulk-upload-check",
                json={"assets": [{"id": i, "checksum": c} for i, c in batch]},
            )
            for row in data["results"]:
                results[row["id"]] = CheckResult(
                    action=CheckAction(row["action"]),
                    reason=row.get("reason"),
                    existing_asset=row.get("assetId"),
                    is_trashed=bool(row.get("isTrashed", False)),
                )
        missing = {i for i, _ in pending} - set(results)
        if missing:
            raise ImmichError(f"Immich returned no answer for {len(missing)} files")
        return results

    def upload(
        self,
        stream: Readable,
        filename: str,
        size: int,
        sha1: str,
        captured: datetime,
        sidecar: bytes | None = None,
        favorite: bool = False,
        progress: Callable[[int], None] | None = None,
    ) -> UploadResult:
        """Stream one file to Immich without holding it in memory.

        The SHA-1 and size are checked while sending; on any mismatch the request is abandoned
        before it completes, so Immich stores nothing.
        """
        boundary = uuid.uuid4().hex
        created = captured.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        fields = {
            "deviceAssetId": f"googich-{sha1}",
            "deviceId": DEVICE_ID,
            "fileCreatedAt": created,
            "fileModifiedAt": created,
            "isFavorite": "true" if favorite else "false",
        }
        head = b"".join(_field(boundary, k, v) for k, v in fields.items())
        head += _file_header(boundary, "assetData", filename, "application/octet-stream")
        tail = b"\r\n"
        if sidecar is not None:
            tail += _file_header(boundary, "sidecarData", filename + ".xmp", "application/xml")
            tail += sidecar + b"\r\n"
        tail += f"--{boundary}--\r\n".encode()

        def body() -> Iterator[bytes]:
            yield head
            digest = hashlib.sha1(usedforsecurity=False)
            sent = 0
            while chunk := stream.read(UPLOAD_CHUNK):
                digest.update(chunk)
                sent += len(chunk)
                if sent > size:
                    raise UploadIntegrityError(f"{filename}: larger than when scanned")
                if progress:
                    progress(len(chunk))
                yield chunk
            if sent != size or digest.hexdigest() != sha1:
                raise UploadIntegrityError(f"{filename}: content changed since it was scanned")
            yield tail

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(head) + size + len(tail)),
        }
        data = self._request("POST", "/assets", content=body(), headers=headers)
        return UploadResult(asset_id=str(data["id"]), duplicate=data.get("status") == "duplicate")

    def asset_dates(self, asset_id: str) -> AssetDates:
        data = self._request("GET", f"/assets/{asset_id}")
        exif = data.get("exifInfo") or {}
        return AssetDates(
            date_time_original=_parse_time(exif.get("dateTimeOriginal"), aware=True),
            local_date_time=_parse_time(data.get("localDateTime"), aware=False),
            time_zone=exif.get("timeZone"),
        )

    def _request(
        self,
        method: str,
        path: str,
        json: object = None,
        content: Iterator[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.request(
                method, path, json=json, content=content, headers=headers
            )
        except UploadIntegrityError:
            raise
        except httpx.HTTPError as error:
            raise ImmichError(f"cannot reach Immich: {type(error).__name__}") from None
        if response.status_code == 401:
            raise ImmichError("Immich rejected the API key (401)")
        if response.status_code == 403:
            raise ImmichError(f"API key lacks permission for {method} {path} (403)")
        if response.is_error:
            raise ImmichError(f"Immich answered {response.status_code} for {method} {path}")
        try:
            data = response.json()
        except ValueError:
            raise ImmichError(f"Immich sent invalid JSON for {method} {path}") from None
        if not isinstance(data, dict):
            raise ImmichError(f"unexpected response for {method} {path}")
        return data


def _field(boundary: str, name: str, value: str) -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    ).encode()


def _file_header(boundary: str, name: str, filename: str, content_type: str) -> bytes:
    safe = filename.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"; filename="{safe}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()


def _parse_time(value: object, aware: bool) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if aware:
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return parsed.replace(tzinfo=None)  # Immich marks local wall-clock time with Z
