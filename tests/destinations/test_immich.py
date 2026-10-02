import json
from pathlib import Path

import httpx
import pytest

from googich_takeaway.destinations import immich
from googich_takeaway.destinations.immich import (
    CheckAction,
    ImmichClient,
    ImmichError,
    read_api_key,
)

KEY = "test-key-not-real"


class FakeImmich:
    """Answers like Immich 2.7.5 for the endpoints the client uses."""

    def __init__(self, known: dict[str, bool] | None = None, status: int = 200) -> None:
        self.known = known or {}  # sha1 -> is_trashed
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "nope"})
        if request.headers.get("x-api-key") != KEY:
            return httpx.Response(401, json={"message": "Invalid API key"})
        if request.url.path == "/api/server/version":
            return httpx.Response(200, json={"major": 2, "minor": 7, "patch": 5})
        if request.url.path == "/api/assets/bulk-upload-check":
            body = json.loads(request.content)
            results = []
            for asset in body["assets"]:
                if asset["checksum"] in self.known:
                    results.append(
                        {
                            "id": asset["id"],
                            "action": "reject",
                            "reason": "duplicate",
                            "assetId": "existing-" + asset["id"],
                            "isTrashed": self.known[asset["checksum"]],
                        }
                    )
                else:
                    results.append({"id": asset["id"], "action": "accept"})
            return httpx.Response(200, json={"results": results})
        return httpx.Response(404)


def client(fake: FakeImmich, key: str = KEY) -> ImmichClient:
    return ImmichClient("http://immich.test:2283/", key, transport=httpx.MockTransport(fake))


def test_version_and_key_header() -> None:
    fake = FakeImmich()
    with client(fake) as c:
        assert c.server_version() == "2.7.5"
    assert fake.requests[0].headers["x-api-key"] == KEY
    assert str(fake.requests[0].url) == "http://immich.test:2283/api/server/version"


def test_check_existing_reports_new_present_and_trashed() -> None:
    fake = FakeImmich(known={"aa": False, "bb": True})
    with client(fake) as c:
        results = c.check_existing([("1", "aa"), ("2", "bb"), ("3", "cc")])
    assert results["1"].action is CheckAction.REJECT
    assert results["1"].reason == "duplicate"
    assert not results["1"].is_trashed
    assert results["2"].is_trashed
    assert results["3"].action is CheckAction.ACCEPT


def test_check_existing_batches_large_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(immich, "CHECK_BATCH", 2)
    fake = FakeImmich()
    with client(fake) as c:
        results = c.check_existing([(str(i), f"{i:040x}") for i in range(5)])
    assert len(results) == 5
    assert len(fake.requests) == 3


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "rejected the API key"), (403, "lacks permission"), (500, "answered 500")],
)
def test_http_errors_are_explained(status: int, message: str) -> None:
    with client(FakeImmich(status=status)) as c, pytest.raises(ImmichError, match=message):
        c.server_version()


def test_wrong_key_is_rejected() -> None:
    with client(FakeImmich(), key="wrong") as c, pytest.raises(ImmichError, match="401"):
        c.server_version()


def test_errors_never_contain_the_key() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed with headers {request.headers}")

    c = ImmichClient("http://immich.test", KEY, transport=httpx.MockTransport(broken))
    with pytest.raises(ImmichError) as caught:
        c.server_version()
    assert KEY not in str(caught.value)
    assert caught.value.__cause__ is None  # the original error, which held the key, is dropped


def test_read_api_key(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.write_text(KEY + "\n")
    path.chmod(0o600)
    assert read_api_key(path) == KEY


def test_read_api_key_refuses_readable_files(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.write_text(KEY)
    path.chmod(0o644)
    with pytest.raises(ImmichError, match="chmod 600"):
        read_api_key(path)


def test_read_api_key_refuses_empty_files(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.write_text("\n")
    path.chmod(0o600)
    with pytest.raises(ImmichError, match="empty"):
        read_api_key(path)


def test_a_refused_request_says_why() -> None:
    import httpx

    from googich_takeaway.destinations.immich import ImmichClient, ImmichError

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"message": "Unsupported file type", "error": "Bad Request"}
        )

    client = ImmichClient("http://immich.test", "key", transport=httpx.MockTransport(handle))
    with pytest.raises(ImmichError, match="400 for GET /assets/x: Unsupported file type"):
        client.asset_dates("x")
