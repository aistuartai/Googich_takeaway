"""scripts/immich_api.py: the daily check of Immich's published API against what the app uses.

The fixtures are Immich's own API descriptions for the oldest supported and the newest tested
release, cut down to the endpoints this app calls."""

import copy
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "immich_api"
_loader = importlib.util.spec_from_file_location("immich_api", ROOT / "scripts" / "immich_api.py")
assert _loader
assert _loader.loader
immich_api = importlib.util.module_from_spec(_loader)
_loader.loader.exec_module(immich_api)


def _spec(version: str) -> dict[str, Any]:
    return dict(json.loads((FIXTURES / f"{version}.json").read_text()))


@pytest.mark.parametrize("version", ["v2.7.5", "v3.3.3"])
def test_the_supported_versions_meet_the_contract(version: str) -> None:
    assert immich_api.check(_spec(version)) == []


def test_the_fixtures_are_the_supported_range() -> None:
    from googich_takeaway.destinations.immich import IMMICH_MINIMUM, IMMICH_TESTED

    for wanted in (IMMICH_MINIMUM, IMMICH_TESTED):
        name = "v" + ".".join(map(str, wanted))
        assert (FIXTURES / f"{name}.json").exists(), f"add the {name} API description fixture"


def test_changes_that_matter_are_reported() -> None:
    spec = copy.deepcopy(_spec("v3.3.3"))
    schemas = spec["components"]["schemas"]
    album = schemas["AlbumResponseDto"]["properties"]
    album["name"] = album.pop("albumName")  # renamed
    schemas["AssetMediaCreateDto"]["properties"].pop("sidecarData")  # removed
    spec["paths"]["/stacks"]["post"]["deprecated"] = True
    for parameter in spec["paths"]["/albums"]["get"]["parameters"]:
        if parameter["name"] == "isOwned":  # same name, other meaning
            parameter["description"] = "Filter: true = owned or shared"
    schemas["Permission"]["enum"].remove("tag.asset")
    del spec["paths"]["/tags/{id}/assets"]
    problems = "\n".join(immich_api.check(spec))
    assert "[].albumName, is gone" in problems
    assert "sidecarData, is gone" in problems
    assert "POST /stacks: marked deprecated" in problems
    assert "isOwned no longer says 'true = only owned'" in problems
    assert "permission tag.asset is gone" in problems
    assert "PUT /tags/{id}/assets: no longer exists" in problems


def test_a_lost_value_is_reported() -> None:
    spec = copy.deepcopy(_spec("v3.3.3"))
    status = spec["components"]["schemas"]["AssetMediaStatus"]
    status["enum"] = [v for v in status["enum"] if v != "duplicate"]
    assert any("no longer allows 'duplicate'" in p for p in immich_api.check(spec))


def test_the_contract_lists_every_endpoint_the_client_calls() -> None:
    """A new call in the Immich client must be added to the contract, or it goes unwatched."""
    client = (ROOT / "src" / "googich_takeaway" / "destinations" / "immich.py").read_text()
    called = {
        (method.lower(), re.sub(r"\{[^}]+\}", "{id}", path))
        for method, path in re.findall(
            r'_request(?:_any)?\(\s*"(GET|POST|PUT|PATCH|DELETE)",\s*f?"(/[^"]+)"', client
        )
    }
    listed = {
        (method, entry[method])
        for entry in immich_api.CONTRACT
        for method in immich_api.METHODS
        if method in entry
    }
    assert called, "no calls found: the pattern above needs updating"
    assert called <= listed, f"not in the contract: {sorted(called - listed)}"
