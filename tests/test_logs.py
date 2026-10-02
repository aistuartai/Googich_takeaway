import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from googich_takeaway import logs as logs_module
from googich_takeaway.logs import LogBuffer, redact, setup_logging

# Fake secrets are assembled at runtime so the source never contains anything shaped like a
# real credential; the repository's secret scanner would rightly flag those.
PEM_START = "-----BEGIN " + "PRIVATE KEY-----"
PEM_END = "-----END " + "PRIVATE KEY-----"
FAKE_KEY = "fake" + "-immich-" + "value"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("headers {'Authorization': 'Bearer ya29.a0AfH6SMBx'}", "ya29.a0AfH6SMBx"),
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ("{'x-api-key': '" + FAKE_KEY + "'}", FAKE_KEY),
        ('{"private_key": "' + PEM_START + '\\nFAKEBODYA"}', "FAKEBODYA"),
        (PEM_START + "\nFAKEBODYB\n" + PEM_END, "FAKEBODYB"),
        ("sending to ntfys://tok3n@ntfy.example/photos", "tok3n"),
        ("mailtos://user:hunter2@mail.example", "hunter2"),
        ("GET https://x.example/a?access_token=s3cr3t&b=1", "s3cr3t"),
    ],
)
def test_redaction(text: str, secret: str) -> None:
    cleaned = redact(text)
    assert secret not in cleaned
    assert "redacted" in cleaned


def test_redaction_leaves_ordinary_text_alone() -> None:
    text = "Uploaded IMG_0001.jpg (2.1 MB) to http://immich:2283/api/assets"
    assert redact(text) == text


def test_buffer_filters() -> None:
    buffer = LogBuffer(capacity=10)
    log = logging.getLogger("googich.test.buffer")
    log.addHandler(buffer)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.debug("detail one")
    log.info("downloaded takeout-001.zip")
    log.warning("retrying after a dropped connection")
    assert [e.message for e in buffer.query(level="INFO")] == [
        "downloaded takeout-001.zip",
        "retrying after a dropped connection",
    ]
    assert [e.level for e in buffer.query(level="WARNING")] == ["WARNING"]
    assert [e.message for e in buffer.query(level="DEBUG", text="TAKEOUT")] == [
        "downloaded takeout-001.zip"
    ]
    first = buffer.query(level="DEBUG")[0]
    assert len(buffer.query(level="DEBUG", after=first.seq)) == 2
    future = datetime.now(UTC) + timedelta(hours=1)
    assert buffer.query(level="DEBUG", since=future) == []


def test_buffer_keeps_only_recent_records() -> None:
    buffer = LogBuffer(capacity=3)
    log = logging.getLogger("googich.test.capacity")
    log.addHandler(buffer)
    log.propagate = False
    log.setLevel(logging.INFO)
    for number in range(5):
        log.info("line %d", number)
    assert [e.message for e in buffer.query()] == ["line 2", "line 3", "line 4"]


@pytest.fixture
def clean_root() -> object:
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


def test_setup_writes_redacted_json_lines_owner_only(
    tmp_path: Path, clean_root: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logs_module, "LOG_FILE_BYTES", 300)
    stored = setup_logging(tmp_path / "state.db")
    log = logging.getLogger("googich.test.file")
    for _ in range(10):
        log.info("using x-api-key: sup3rs3cret for upload")
    try:
        raise ValueError("Bearer abcdefghijklmnopqrstuvwxyz")
    except ValueError:
        log.exception("failed")
    files = stored.files()
    assert len(files) > 1  # rotated
    for path in files:
        assert path.stat().st_mode & 0o077 == 0
        text = path.read_text()
        assert "sup3rs3cret" not in text
        assert "abcdefghijklmnopqrstuvwxyz" not in text
    line = json.loads((stored.directory / "googich.log").read_text().splitlines()[-1])
    assert set(line) == {"time", "level", "logger", "message"}
    assert "Traceback" in line["message"]
    assert any("[redacted]" in e.message for e in stored.buffer.query())


def test_polling_requests_are_not_logged_but_pages_and_failures_are() -> None:
    import logging

    from googich_takeaway.logs import _QuietAccessFilter

    quiet = _QuietAccessFilter()

    def access(path: str, status: int) -> bool:
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
            ("192.0.2.1:1234", "GET", path, "1.1", status), None,
        )  # fmt: skip
        return quiet.filter(record)

    assert not access("/logs/tail?after=204&level=INFO&q=", 200)
    assert not access("/status", 200)
    assert not access("/activity", 200)
    assert not access("/static/app.css", 304)
    assert access("/logs/tail?after=1", 500)  # failures are kept
    assert access("/logs", 200)  # pages opened are kept
    assert access("/runs", 303)
