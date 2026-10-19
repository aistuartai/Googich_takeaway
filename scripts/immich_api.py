"""Check an Immich release's API against what this app uses.

Immich publishes its API as OpenAPI with every release. This compares one release's against the
contract below: every endpoint the app calls, the fields it sends and reads, the values it
expects, the permissions it asks for, and the meaning of the album filter (which changed in
Immich 3: with no filter, 2.x listed only owned albums, 3.x lists all, shared-with-me too).

    python3 scripts/immich_api.py latest            # the newest Immich release
    python3 scripts/immich_api.py minimum tested    # the range this release supports
    python3 scripts/immich_api.py v3.4.0 --spec path/to/immich-openapi-specs.json

Exits 1, listing what changed, when anything the app relies on is missing, renamed, has
different values, is marked deprecated, or means something else. Standard library only.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
CLIENT = ROOT / "src" / "googich_takeaway" / "destinations" / "immich.py"
REPOSITORY = "immich-app/immich"
SPEC_PATH = "open-api/immich-openapi-specs.json"

# Each endpoint: what is sent ("query", "body") and read ("response"). A field is a dotted path;
# "[]" steps into a list ("results[].id", or "[].albumName" for a list answer). "name=a|b"
# also needs those values among the field's allowed ones. "since"/"before" limit an entry to
# the Immich versions it applies to. "means" pins a parameter's description to words that must
# stay in it, so a change of meaning is caught, not only a missing field.
CONTRACT: list[dict[str, Any]] = [
    {"get": "/server/version", "response": ["major", "minor", "patch"]},
    {"get": "/api-keys/me", "response": ["permissions"]},
    {"get": "/assets/statistics", "query": ["isTrashed"], "response": ["total"]},
    {
        "post": "/assets/bulk-upload-check",
        "body": ["assets[].id", "assets[].checksum"],
        "response": [
            "results[].id",
            "results[].action=accept|reject",
            "results[].reason=duplicate",
            "results[].assetId",
            "results[].isTrashed",
        ],
    },
    {
        "post": "/assets",
        "body": [
            "assetData",
            "fileCreatedAt",
            "fileModifiedAt",
            "isFavorite",
            "visibility=archive",
            "sidecarData",
        ],
        "response": ["id", "status=created|duplicate"],
    },
    {
        "get": "/albums",
        "before": "3.0.0",
        "query": ["shared"],
        "means": {"shared": "undefined = all owned albums"},
        "response": ["[].id", "[].albumName"],
    },
    {
        "get": "/albums",
        "since": "3.0.0",
        "query": ["isOwned"],
        "means": {"isOwned": "true = only owned"},
        "response": ["[].id", "[].albumName"],
    },
    {"post": "/albums", "body": ["albumName", "description"], "response": ["id"]},
    {"put": "/albums/{id}/assets", "body": ["ids"]},
    {"post": "/stacks", "body": ["assetIds"], "response": ["id"]},
    {"put": "/tags", "body": ["tags"], "response": ["[].id", "[].value"]},
    {"put": "/tags/{id}/assets", "body": ["ids"]},
    {"delete": "/assets", "body": ["ids", "force"]},
    {
        "get": "/assets/{id}",
        "response": ["localDateTime", "exifInfo.dateTimeOriginal", "exifInfo.timeZone"],
    },
]
PERMISSIONS = [
    "all", "asset.upload", "asset.read", "asset.delete", "asset.statistics", "album.read",
    "album.create", "albumAsset.create", "stack.create", "tag.create", "tag.asset",
]  # fmt: skip
METHODS = ("get", "post", "put", "patch", "delete")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def _client_version(name: str) -> str:
    """IMMICH_MINIMUM or IMMICH_TESTED from the app's Immich client, as ``vX.Y.Z``."""
    found = re.search(rf"^{name} = \((\d+), (\d+), (\d+)\)", CLIENT.read_text(), re.M)
    if not found:
        raise SystemExit(f"{name} not found in {CLIENT}")
    return "v" + ".".join(found.groups())


def _latest_tag() -> str:
    """The newest Immich release, from where GitHub's latest-release page points."""

    class Stay(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    try:
        urllib.request.build_opener(Stay).open(
            f"https://github.com/{REPOSITORY}/releases/latest", timeout=30
        )
    except urllib.error.HTTPError as answer:
        found = re.search(r"/releases/tag/(v[\d.]+)$", answer.headers.get("location", ""))
        if answer.code in (301, 302) and found:
            return found[1]
    raise SystemExit("could not find Immich's latest release")


def _fetch(tag: str) -> dict[str, Any]:
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{tag}/{SPEC_PATH}"
    with urllib.request.urlopen(url, timeout=60) as answer:
        return dict(json.loads(answer.read()))


class Spec:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.schemas: dict[str, Any] = data.get("components", {}).get("schemas", {})
        self.version = _version(str(data.get("info", {}).get("version", "0")))

    def resolve(self, schema: Any) -> dict[str, Any]:
        """A schema with its $ref followed, and allOf/oneOf/anyOf merged into one."""
        seen = 0
        while isinstance(schema, dict) and "$ref" in schema and seen < 20:
            schema = self.schemas.get(schema["$ref"].rsplit("/", 1)[-1], {})
            seen += 1
        if not isinstance(schema, dict):
            return {}
        merged = dict(schema)
        for key in ("allOf", "oneOf", "anyOf"):
            for part in schema.get(key, []):
                resolved = self.resolve(part)
                merged.setdefault("properties", {}).update(resolved.get("properties", {}))
                if "enum" in resolved:
                    merged["enum"] = list(merged.get("enum", [])) + list(resolved["enum"])
                for inner in ("items", "type", "$ref"):
                    if inner in resolved and inner not in merged:
                        merged[inner] = resolved[inner]
        return merged

    def field(self, schema: Any, path: str) -> dict[str, Any] | None:
        """The schema at a dotted path ("a.b", "a[].b", "[].b"), or None if it is not there."""
        current = self.resolve(schema)
        for step in re.findall(r"\[\]|[^.\[\]]+", path):
            if step == "[]":
                if "items" not in current:
                    return None
                current = self.resolve(current["items"])
            else:
                props = current.get("properties", {})
                if step not in props:
                    return None
                current = self.resolve(props[step])
        return current

    def enum_values(self, schema: dict[str, Any]) -> set[str]:
        if "enum" in schema:
            return {str(v) for v in schema["enum"]}
        if "items" in schema:
            return self.enum_values(self.resolve(schema["items"]))
        return set()


def check(data: dict[str, Any]) -> list[str]:
    """What in this API description breaks the contract; empty if nothing."""
    spec = Spec(data)
    paths: dict[str, Any] = data.get("paths", {})
    problems: list[str] = []
    for entry in CONTRACT:
        if "since" in entry and spec.version < _version(entry["since"]):
            continue
        if "before" in entry and spec.version >= _version(entry["before"]):
            continue
        method = next(m for m in METHODS if m in entry)
        path = entry[method]
        name = f"{method.upper()} {path}"
        operation = paths.get(path, {}).get(method)
        if operation is None:
            problems.append(f"{name}: no longer exists")
            continue
        if operation.get("deprecated"):
            problems.append(f"{name}: marked deprecated (it may be removed next)")
        parameters = {p.get("name"): p for p in operation.get("parameters", [])}
        for wanted in entry.get("query", []):
            if wanted not in parameters:
                problems.append(f"{name}: query parameter {wanted} is gone")
        for wanted, words in entry.get("means", {}).items():
            text = str(parameters.get(wanted, {}).get("description", ""))
            if words not in text:
                problems.append(f"{name}: {wanted} no longer says {words!r}; now: {text!r}")
        bodies = operation.get("requestBody", {}).get("content", {})
        body = next(iter(bodies.values()), {}).get("schema") if bodies else None
        problems += _fields(spec, name, "sends", body, entry.get("body", []))
        answers = [
            r for code, r in operation.get("responses", {}).items() if str(code).startswith("2")
        ]
        content = next((r.get("content", {}) for r in answers if r.get("content")), {})
        answer = next(iter(content.values()), {}).get("schema") if content else None
        problems += _fields(spec, name, "reads", answer, entry.get("response", []))
    known = spec.enum_values(spec.resolve(spec.schemas.get("Permission", {})))
    problems += [f"permission {p} is gone" for p in PERMISSIONS if known and p not in known]
    if not known:
        problems.append("the Permission list is gone")
    return problems


def _fields(spec: Spec, name: str, how: str, schema: Any, wanted: list[str]) -> Iterator[str]:
    for item in wanted:
        path, _, values = item.partition("=")
        found = spec.field(schema, path) if schema is not None else None
        if found is None:
            yield f"{name}: the field it {how}, {path}, is gone"
            continue
        if values:
            allowed = spec.enum_values(found)
            for value in values.split("|"):
                if allowed and value not in allowed:
                    yield f"{name}: {path} no longer allows {value!r} (now {sorted(allowed)})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "versions", nargs="+", help="Immich tags such as v3.3.3, or latest, minimum, tested"
    )
    parser.add_argument("--spec", type=Path, help="read this file instead (one version only)")
    args = parser.parse_args(argv)
    failed = False
    for asked in args.versions:
        tag = {
            "latest": _latest_tag,
            "minimum": lambda: _client_version("IMMICH_MINIMUM"),
            "tested": lambda: _client_version("IMMICH_TESTED"),
        }.get(asked, lambda a=asked: a)()
        data = json.loads(args.spec.read_text()) if args.spec else _fetch(tag)
        problems = check(data)
        if problems:
            failed = True
            print(f"Immich {tag} ({asked}): {len(problems)} change(s) this app relies on:")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"Immich {tag} ({asked}): everything this app uses is as expected.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
