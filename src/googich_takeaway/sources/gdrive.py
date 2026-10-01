"""Google Drive source: a folder shared with a service account.

Access is read-only by design. The service account is given Viewer access to the Takeout folder
only, and the token is requested with the ``drive.readonly`` scope, so the app can neither change
nor delete anything in Drive. The key file is read from disk and never logged or printed.
"""

import json
import re
import stat
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import Request as AuthRequest
from google.auth.transport import Response as AuthResponse
from google.oauth2 import service_account

from googich_takeaway.sources.base import RemoteFile, SourceError, TransientSourceError

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
API = "https://www.googleapis.com/drive/v3"
ARCHIVE_SUFFIXES = (".zip", ".tgz", ".tar.gz")
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_FIELDS = "nextPageToken,files(id,name,size,modifiedTime,sha256Checksum,md5Checksum,webViewLink)"


def load_service_account(path: Path) -> dict[str, Any]:
    """Read a service account key file, refusing files other users can read."""
    mode = path.stat().st_mode
    if stat.S_IMODE(mode) & 0o077:
        raise SourceError(f"{path} is readable by other users; run: chmod 600 {path}")
    try:
        info = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceError(f"{path} is not a readable service account key file") from error
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise SourceError(f"{path} is not a service account key file")
    return info


class _AuthTransport(AuthRequest):
    """Lets google-auth fetch tokens through httpx."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **_: object,
    ) -> AuthResponse:
        try:
            response = self._client.request(
                method, url, content=body, headers=dict(headers or {}), timeout=timeout or 30
            )
        except httpx.HTTPError as error:
            raise TransientSourceError(f"cannot reach Google: {type(error).__name__}") from None
        return _AuthResponse(response)


class _AuthResponse(AuthResponse):
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    @property
    def data(self) -> bytes:
        return self._response.content


class GoogleDriveSource:
    def __init__(
        self,
        folder_id: str,
        service_account_info: dict[str, Any],
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.name = f"gdrive:{folder_id}"
        self.folder_id = folder_id
        self._client = httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=True)
        try:
            self._credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
                service_account_info, scopes=[SCOPE]
            )
        except (ValueError, KeyError, GoogleAuthError):
            raise SourceError("the service account key file is invalid") from None
        self._auth = _AuthTransport(self._client)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GoogleDriveSource":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def account(self) -> str:
        """Service account email, to share the Drive folder with."""
        return str(self._credentials.service_account_email)

    def check_folder(self) -> None:
        """Fail clearly if the folder is missing or not shared with the service account."""
        url = f"{API}/files/{_path_segment(self.folder_id)}"
        params = {"fields": "id,mimeType,trashed", "supportsAllDrives": "true"}
        try:
            response = self._client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as error:
            raise TransientSourceError(
                f"cannot reach Google Drive: {type(error).__name__}"
            ) from None
        if response.status_code == 404:
            raise SourceError(
                f"the Drive folder was not found, or is not shared with {self.account}. "
                "Share it with that address as Viewer, and check the folder ID."
            )
        _raise_for_status(response, "folder")
        data = response.json()
        if data.get("mimeType") != "application/vnd.google-apps.folder":
            raise SourceError("the Drive folder ID points to a file, not a folder")
        if data.get("trashed"):
            raise SourceError("the Drive folder is in the trash")

    def list_archives(self) -> list[RemoteFile]:
        self.check_folder()
        found: list[RemoteFile] = []
        page: str | None = None
        query = f"'{_quote(self.folder_id)}' in parents and trashed = false"
        while True:
            params = {
                "q": query,
                "fields": _FIELDS,
                "pageSize": "1000",
                "orderBy": "name",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page:
                params["pageToken"] = page
            data = self._get_json(f"{API}/files", params)
            for item in data.get("files", []):
                if not str(item.get("name", "")).lower().endswith(ARCHIVE_SUFFIXES):
                    continue
                found.append(
                    RemoteFile(
                        file_id=item["id"],
                        name=item["name"],
                        size=int(item.get("size", 0)),
                        modified=datetime.fromisoformat(item["modifiedTime"]),
                        sha256=item.get("sha256Checksum"),
                        md5=item.get("md5Checksum"),
                        link=item.get("webViewLink"),
                    )
                )
            page = data.get("nextPageToken")
            if not page:
                return found

    def read(self, file: RemoteFile, start: int = 0) -> Iterator[bytes]:
        headers = self._headers()
        if start:
            headers["Range"] = f"bytes={start}-"
        url = f"{API}/files/{file.file_id}"
        params = {"alt": "media", "supportsAllDrives": "true"}
        try:
            with self._client.stream("GET", url, params=params, headers=headers) as response:
                _raise_for_status(response, file.name)
                if start and response.status_code != 206:
                    raise SourceError(f"{file.name}: Drive ignored the resume request")
                # No fixed chunk size: buffering would lose bytes already received if the
                # connection drops, and resume would have to fetch them again.
                yield from response.iter_bytes()
        except httpx.HTTPError as error:
            raise TransientSourceError(
                f"{file.name}: download interrupted ({type(error).__name__})"
            ) from None

    def _headers(self) -> dict[str, str]:
        if not self._credentials.valid:
            try:
                self._credentials.refresh(self._auth)
            except GoogleAuthError as error:
                raise SourceError(f"Google refused the service account: {error}") from None
        return {"Authorization": f"Bearer {self._credentials.token}"}

    def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as error:
            raise TransientSourceError(
                f"cannot reach Google Drive: {type(error).__name__}"
            ) from None
        _raise_for_status(response, "folder listing")
        data = response.json()
        if not isinstance(data, dict):
            raise SourceError("unexpected answer from Google Drive")
        return data


def _raise_for_status(response: httpx.Response, what: str) -> None:
    code = response.status_code
    if code < 400:
        return
    if code in (429, 500, 502, 503, 504):
        raise TransientSourceError(f"{what}: Google Drive answered {code}; will retry")
    if code == 404:
        raise SourceError(
            f"{what}: not found (404). Check the folder ID and that the folder is shared "
            "with the service account."
        )
    reasons = _error_reasons(response)
    if "accessNotConfigured" in reasons or "SERVICE_DISABLED" in reasons:
        raise SourceError(
            "the Google Drive API is not enabled in the service account's Google Cloud project. "
            "Enable it under APIs & Services > Library > Google Drive API, wait a few minutes, "
            "then try again."
        )
    if reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
        raise TransientSourceError(f"{what}: Google Drive rate limit; will retry")
    if code in (401, 403):
        detail = f" ({', '.join(sorted(reasons))})" if reasons else ""
        raise SourceError(
            f"{what}: access denied ({code}){detail}. Check the folder is shared with the "
            "service account as Viewer."
        )
    raise SourceError(f"{what}: Google Drive answered {code}")


def _error_reasons(response: httpx.Response) -> set[str]:
    """Machine-readable reasons from a Google API error body; never its free text."""
    try:
        response.read()
        error = response.json().get("error", {})
    except (ValueError, AttributeError, httpx.HTTPError):
        return set()
    if not isinstance(error, dict):
        return set()
    found = {str(e.get("reason")) for e in error.get("errors", []) if isinstance(e, dict)}
    found |= {str(d.get("reason")) for d in error.get("details", []) if isinstance(d, dict)}
    return {r for r in found if r and r != "None"}


def _path_segment(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise SourceError("the Drive folder ID contains unexpected characters")
    return value


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
