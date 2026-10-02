from collections.abc import Iterator

import pytest

from googich_takeaway.locations import close_shared_smb


@pytest.fixture(autouse=True)
def _fresh_smb_pool() -> Iterator[None]:
    """Each test gets its own SMB connections; the pool is process-wide in the app."""
    close_shared_smb()
    yield
    close_shared_smb()
