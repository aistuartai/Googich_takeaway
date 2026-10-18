"""Googich Takeaway: move Google Photos into Immich using Google Takeout archives."""

import re
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("googich-takeaway")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"
# Python writes a release candidate 1.2.3rc1; its tag and image are 1.2.3-rc1, and so is this.
__version__ = re.sub(r"^(\d+\.\d+\.\d+)rc(\d+)$", r"\1-rc\2", __version__)
