"""Immich API client: duplicate checks now, uploads later.

The API key is read from a file and only ever placed in the ``x-api-key`` request header. It is
never logged, printed or included in exception messages.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

CHECK_BATCH = 1000
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


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

    def _request(self, method: str, path: str, json: object = None) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, json=json)
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
