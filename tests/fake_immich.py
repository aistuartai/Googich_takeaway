"""An in-memory stand-in for the Immich endpoints the importer uses.

It behaves like Immich 2.7.5 as observed in the spike: an uploaded XMP sidecar's
DateTimeOriginal (with offset) becomes the asset's capture instant and local display time, and
metadata appears only after a short delay.
"""

import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

KEY = "test-key-not-real"


@dataclass
class Asset:
    id: str
    sha1: str
    filename: str
    data: bytes
    xmp: bytes | None
    favorite: bool
    trashed: bool = False
    reads: int = 0
    archived: bool = False


@dataclass
class FakeImmichServer:
    metadata_delay_reads: int = 0
    """GET /assets/{id} returns no exifInfo this many times before it does."""
    refuse_names: frozenset[str] = frozenset()
    """File names answered with 400 "Unsupported file type", as Immich does for .MP files."""
    fail_uploads: int = 0
    """Answer this many uploads with 500 (after storing them, like a lost response)."""
    store_failed_uploads: bool = True
    shift_hours: int = 0
    """Report capture times shifted by this much, to simulate a date mismatch."""
    permissions: list[str] = field(
        default_factory=lambda: ["asset.upload", "asset.read", "stack.create"]
    )
    assets: dict[str, Asset] = field(default_factory=dict)
    albums: dict[str, tuple[str, set[str]]] = field(default_factory=dict)
    """Album id: (name, asset ids), owned by the key's user."""
    shared_with_me: dict[str, tuple[str, set[str]]] = field(default_factory=dict)
    """Albums another user shared with the key's user: listed as Immich of this version does."""
    version: tuple[int, int, int] = (2, 7, 5)
    """Which Immich this answers like: 3.x lists shared-with-me albums unless asked isOwned."""
    stacks: list[list[str]] = field(default_factory=list)
    tags: dict[str, tuple[str, set[str]]] = field(default_factory=dict)
    """Tag id: (value, asset ids)."""
    requests: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def by_sha1(self, sha1: str) -> Asset | None:
        return next((a for a in self.assets.values() if a.sha1 == sha1), None)

    def preload(self, data: bytes, trashed: bool = False) -> Asset:
        sha1 = hashlib.sha1(data, usedforsecurity=False).hexdigest()
        asset = Asset(str(uuid.uuid4()), sha1, "existing.jpg", data, None, False, trashed)
        self.assets[asset.id] = asset
        return asset

    def delete(self, sha1: str) -> None:
        asset = self.by_sha1(sha1)
        assert asset is not None
        del self.assets[asset.id]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        # Uploads arrive from several threads at once; a real server copes, this fake's plain
        # dicts and counters need one request at a time.
        with self._lock:
            return self._handle(request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if request.headers.get("x-api-key") != KEY:
            return httpx.Response(401, json={"message": "Invalid API key"})
        path = request.url.path
        if path == "/api/server/version":
            major, minor, patch = self.version
            return httpx.Response(200, json={"major": major, "minor": minor, "patch": patch})
        if path == "/api/api-keys/me":
            return httpx.Response(200, json={"name": "test", "permissions": self.permissions})
        if path == "/api/assets/statistics":
            if not {"asset.statistics", "all"} & set(self.permissions):
                return httpx.Response(403, json={"message": "Missing permission"})
            trashed = request.url.params.get("isTrashed") == "true"
            kept = [a for a in self.assets.values() if a.trashed == trashed]
            return httpx.Response(200, json={"images": len(kept), "videos": 0, "total": len(kept)})
        if path == "/api/assets/bulk-upload-check":
            return self._check(json.loads(request.read()))
        if path == "/api/assets" and request.method == "POST":
            return self._upload(request)
        organised = self._organise(request, path)
        if organised is not None:
            return organised
        found = re.fullmatch(r"/api/assets/([0-9a-f-]+)", path)
        if found and request.method == "GET":
            return self._asset(found[1])
        return httpx.Response(404)

    def _allowed(self, permission: str) -> bool:
        return permission in self.permissions or "all" in self.permissions

    def _organise(self, request: httpx.Request, path: str) -> httpx.Response | None:
        """Albums, stacks, tags and trash, as Immich 2.7.5 answers them."""
        method = request.method
        body = json.loads(request.read() or b"null")
        rules = (
            ("GET", r"/api/albums", "album.read"),
            ("POST", r"/api/albums", "album.create"),
            ("PUT", r"/api/albums/([^/]+)/assets", "albumAsset.create"),
            ("POST", r"/api/stacks", "stack.create"),
            ("PUT", r"/api/tags", "tag.create"),
            ("PUT", r"/api/tags/([^/]+)/assets", "tag.asset"),
            ("DELETE", r"/api/assets", "asset.delete"),
        )
        for rule_method, pattern, permission in rules:
            found = re.fullmatch(pattern, path)
            if method != rule_method or not found:
                continue
            if not self._allowed(permission):
                return httpx.Response(403, json={"message": f"Missing permission: {permission}"})
            if path == "/api/albums" and method == "GET":
                listed = [{"id": k, "albumName": v[0]} for k, v in self.albums.items()]
                # 2.x lists owned albums only; 3.x lists every album unless isOwned=true.
                owned_only = self.version < (3, 0, 0) or request.url.params.get("isOwned") == "true"
                if not owned_only:
                    listed += [{"id": k, "albumName": v[0]} for k, v in self.shared_with_me.items()]
                return httpx.Response(200, json=listed)
            if path == "/api/albums":
                album_id = str(uuid.uuid4())
                self.albums[album_id] = (body["albumName"], set())
                return httpx.Response(201, json={"id": album_id, "albumName": body["albumName"]})
            if path.startswith("/api/albums/"):
                album = self.albums.get(found[1])
                if album is None:
                    return httpx.Response(404, json={"message": "Album not found"})
                album[1].update(body["ids"])
                return httpx.Response(200, json=[{"id": i, "success": True} for i in body["ids"]])
            if path == "/api/stacks":
                self.stacks.append(list(body["assetIds"]))
                return httpx.Response(201, json={"id": str(uuid.uuid4())})
            if path == "/api/tags":
                answer = []
                for value in body["tags"]:
                    tag_id = next((k for k, v in self.tags.items() if v[0] == value), None)
                    if tag_id is None:
                        tag_id = str(uuid.uuid4())
                        self.tags[tag_id] = (value, set())
                    answer.append({"id": tag_id, "value": value, "name": value.split("/")[-1]})
                return httpx.Response(200, json=answer)
            if path.startswith("/api/tags/"):
                self.tags[found[1]][1].update(body["ids"])
                return httpx.Response(200, json=[{"id": i, "success": True} for i in body["ids"]])
            for asset_id in body["ids"]:
                self.assets[asset_id].trashed = True
            return httpx.Response(204)
        return None

    def _check(self, body: dict) -> httpx.Response:  # type: ignore[type-arg]
        results = []
        for item in body["assets"]:
            asset = self.by_sha1(item["checksum"])
            if asset:
                results.append(
                    {
                        "id": item["id"],
                        "action": "reject",
                        "reason": "duplicate",
                        "assetId": asset.id,
                        "isTrashed": asset.trashed,
                    }
                )
            else:
                results.append({"id": item["id"], "action": "accept"})
        return httpx.Response(200, json={"results": results})

    def _upload(self, request: httpx.Request) -> httpx.Response:
        body = request.read()  # consumes the streamed body; integrity errors surface here
        declared = int(request.headers["content-length"])
        assert declared == len(body), "Content-Length must match the body"
        parts = _multipart(body, request.headers["content-type"])
        data = parts["assetData"][1]
        sha1 = hashlib.sha1(data, usedforsecurity=False).hexdigest()
        existing = self.by_sha1(sha1)
        if existing:
            return httpx.Response(200, json={"id": existing.id, "status": "duplicate"})
        asset = Asset(
            str(uuid.uuid4()),
            sha1,
            parts["assetData"][0] or "",
            data,
            parts.get("sidecarData", (None, None))[1],
            parts["isFavorite"][1] == b"true",
            archived=parts.get("visibility", (None, b""))[1] == b"archive",
        )
        if asset.filename in self.refuse_names:
            return httpx.Response(400, json={"message": "Unsupported file type"})
        if self.fail_uploads:
            self.fail_uploads -= 1
            if self.store_failed_uploads:
                self.assets[asset.id] = asset
            return httpx.Response(500, json={"message": "lost response"})
        self.assets[asset.id] = asset
        return httpx.Response(201, json={"id": asset.id, "status": "created"})

    def _asset(self, asset_id: str) -> httpx.Response:
        asset = self.assets.get(asset_id)
        if asset is None:
            return httpx.Response(404)
        asset.reads += 1
        if asset.reads <= self.metadata_delay_reads or asset.xmp is None:
            return httpx.Response(200, json={"id": asset.id, "exifInfo": None})
        value = re.search(rb'exif:DateTimeOriginal="([^"]+)"', asset.xmp)
        assert value is not None
        aware = datetime.fromisoformat(value[1].decode())
        local = aware.replace(tzinfo=None).replace(hour=(aware.hour + self.shift_hours) % 24)
        return httpx.Response(
            200,
            json={
                "id": asset.id,
                "localDateTime": local.isoformat() + ".000Z",
                "exifInfo": {
                    "dateTimeOriginal": aware.astimezone(UTC).isoformat(),
                    "timeZone": "UTC" + aware.strftime("%z"),
                },
            },
        )


def _multipart(body: bytes, content_type: str) -> dict[str, tuple[str | None, bytes]]:
    boundary = content_type.split("boundary=", 1)[1].encode()
    parts: dict[str, tuple[str | None, bytes]] = {}
    for chunk in body.split(b"--" + boundary):
        if not chunk.strip() or chunk.strip() == b"--":
            continue
        header, _, data = chunk.lstrip(b"\r\n").partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', header)
        filename = re.search(rb'filename="([^"]*)"', header)
        assert name is not None
        parts[name[1].decode()] = (
            filename[1].decode() if filename else None,
            data.removesuffix(b"\r\n"),
        )
    return parts
