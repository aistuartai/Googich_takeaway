"""An in-memory stand-in for Google's token endpoint and the Drive v3 endpoints used."""

import base64
import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from urllib.parse import parse_qs

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
FOLDER = "folder-123"
FOLDER_NAME = "Takeout exports"
ACCOUNT = "googich@test-project.iam.gserviceaccount.com"


@cache
def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def service_account_info() -> dict[str, str]:
    pem = (
        _key()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    return {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "key-1",
        "private_key": pem,
        "client_email": ACCOUNT,
        "client_id": "1",
        "token_uri": TOKEN_URI,
    }


@dataclass
class DriveFile:
    id: str
    name: str
    data: bytes
    parent: str = FOLDER
    modified: str = "2026-09-30T12:00:00.000Z"
    trashed: bool = False

    def listing(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "size": str(len(self.data)),
            "modifiedTime": self.modified,
            "sha256Checksum": hashlib.sha256(self.data).hexdigest(),
            "md5Checksum": hashlib.md5(self.data, usedforsecurity=False).hexdigest(),
            "webViewLink": f"https://drive.google.com/file/d/{self.id}/view",
        }


class _Dropping(httpx.SyncByteStream):
    def __init__(self, data: bytes, drop_after: int) -> None:
        self.data, self.drop_after = data, drop_after

    def __iter__(self) -> Iterator[bytes]:
        yield self.data[: self.drop_after]
        raise httpx.ReadError("connection reset")


@dataclass
class FakeDrive:
    files: list[DriveFile] = field(default_factory=list)
    page_size: int = 2
    drop_after: dict[str, list[int]] = field(default_factory=dict)
    """file id -> byte counts at which successive downloads are cut off."""
    shared: bool = True
    api_enabled: bool = True
    tokens_issued: int = 0
    requests: list[str] = field(default_factory=list)
    ranges: list[str | None] = field(default_factory=list)

    def add(self, file_id: str, name: str, data: bytes) -> DriveFile:
        file = DriveFile(file_id, name, data)
        self.files.append(file)
        return file

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if str(request.url) == TOKEN_URI:
            return self._token(request)
        if request.headers.get("authorization") != "Bearer fake-token":
            return httpx.Response(401)
        if not self.api_enabled:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "Google Drive API has not been used in project 123 before",
                        "errors": [{"reason": "accessNotConfigured"}],
                        "details": [{"reason": "SERVICE_DISABLED"}],
                    }
                },
            )
        if request.url.path == "/drive/v3/files":
            return self._list(request)
        found = re.fullmatch(r"/drive/v3/files/([^/]+)", request.url.path)
        if found and request.url.params.get("alt") == "media":
            return self._media(found[1], request)
        if found and found[1] == FOLDER:
            if not self.shared:
                return httpx.Response(
                    404, json={"error": {"code": 404, "errors": [{"reason": "notFound"}]}}
                )
            return httpx.Response(
                200,
                json={
                    "id": FOLDER,
                    "name": FOLDER_NAME,
                    "mimeType": "application/vnd.google-apps.folder",
                    "trashed": False,
                },
            )
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        header, claims, signature = form["assertion"][0].split(".")
        _key().public_key().verify(
            _b64(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        body = json.loads(_b64(claims))
        assert body["iss"] == ACCOUNT
        assert body["scope"] == "https://www.googleapis.com/auth/drive.readonly"
        self.tokens_issued += 1
        return httpx.Response(
            200, json={"access_token": "fake-token", "expires_in": 3600, "token_type": "Bearer"}
        )

    def _list(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params["q"]
        assert f"'{FOLDER}' in parents" in query
        assert "trashed = false" in query
        if not self.shared:
            return httpx.Response(200, json={"files": []})
        visible = [f for f in self.files if f.parent == FOLDER and not f.trashed]
        start = int(request.url.params.get("pageToken", "0"))
        page = visible[start : start + self.page_size]
        body: dict[str, object] = {"files": [f.listing() for f in page]}
        if start + self.page_size < len(visible):
            body["nextPageToken"] = str(start + self.page_size)
        return httpx.Response(200, json=body)

    def _media(self, file_id: str, request: httpx.Request) -> httpx.Response:
        file = next((f for f in self.files if f.id == file_id), None)
        if file is None or not self.shared:
            return httpx.Response(404)
        header = request.headers.get("range")
        self.ranges.append(header)
        start = 0
        status = 200
        if header:
            start = int(re.fullmatch(r"bytes=(\d+)-", header)[1])  # type: ignore[index]
            status = 206
        data = file.data[start:]
        cuts = self.drop_after.get(file_id)
        if cuts:
            return httpx.Response(status, stream=_Dropping(data, cuts.pop(0)))
        return httpx.Response(status, content=data)


def _b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
