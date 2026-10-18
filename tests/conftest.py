import ipaddress
import socket
from collections.abc import Iterator

import pytest

from googich_takeaway.locations import close_shared_smb


@pytest.fixture(autouse=True)
def _fresh_smb_pool() -> Iterator[None]:
    """Each test gets its own SMB connections; the pool is process-wide in the app."""
    close_shared_smb()
    yield
    close_shared_smb()


@pytest.fixture(autouse=True)
def _no_internet(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests talk to fakes, never to the internet: a connection anywhere but this machine is
    refused, and fails the test even where the code catches the error (an update check does),
    so a check that forgot its fake (GitHub, Drive, Immich) cannot pass by luck."""
    connect = socket.socket.connect
    attempts: list[str] = []

    def local_only(self: socket.socket, address: object) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            host = str(address[0]) if isinstance(address, tuple) else str(address)
            if not ipaddress.ip_address(host).is_loopback:
                attempts.append(host)
                raise ConnectionRefusedError(f"a test tried to reach {host}: use a fake instead")
        return connect(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", local_only)
    yield
    assert not attempts, f"the test tried to reach the internet: {sorted(set(attempts))}"
