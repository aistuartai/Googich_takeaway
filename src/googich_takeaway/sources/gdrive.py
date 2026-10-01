"""Google Drive source: a folder shared with a service account.

Access is read-only by design. The service account is given Viewer access to the Takeout folder
only, and the token is requested with the ``drive.readonly`` scope, so the app can neither change
nor delete anything in Drive. The key file is read from disk and never logged or printed.
"""

import json
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

    def list_archives(self) -> list[RemoteFile]:
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
    if code in (401, 403):
        raise SourceError(f"{what}: access denied ({code}). Is the folder shared with the account?")
    raise SourceError(f"{what}: Google Drive answered {code}")


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
