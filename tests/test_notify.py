from datetime import UTC, datetime
from pathlib import Path

import pytest

from googich_takeaway.config import Config, ConfigError
from googich_takeaway.credentials import SecretBox
from googich_takeaway.notify import Message, Notifier, Outcome, invalid_urls
from googich_takeaway.state import State

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def test_invalid_urls_are_found_by_line_number() -> None:
    assert invalid_urls(["ntfys://example.invalid/topic", "nonsense://x", ""]) == [2, 3]


def test_outcomes_switched_off_are_not_sent() -> None:
    notifier = Notifier(["json://127.0.0.1:9/"], outcomes=[Outcome.FAILED])
    assert notifier.send(Message(Outcome.SUCCESS, "ok")) is False


def test_undeliverable_notification_never_raises() -> None:
    notifier = Notifier(["json://127.0.0.1:9/"])  # nothing listens on port 9
    assert notifier.send(Message(Outcome.FAILED, "boom", ["detail"]), force=True) is False


def test_without_urls_nothing_is_sent() -> None:
    assert Notifier([]).configured is False
    assert Notifier([]).send(Message(Outcome.FAILED, "x"), force=True) is False


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(State(tmp_path / "state.db"), SecretBox(b"k" * 32), lambda: NOW)


def test_urls_are_sealed_and_outcomes_saved(config: Config, tmp_path: Path) -> None:
    secret_url = "ntfys://mytoken@ntfy.example/photos"  # noqa: S105 - test value
    config.save_notifications(secret_url, ["failed", "paused"])
    assert config.has_notification_urls()
    assert config.notification_outcomes() == {Outcome.FAILED, Outcome.PAUSED}
    dump = "\n".join(__import__("sqlite3").connect(tmp_path / "state.db").iterdump())
    assert "mytoken" not in dump
    assert config.notifier().configured


def test_keep_and_remove_urls(config: Config) -> None:
    config.save_notifications("ntfys://example.invalid/topic", ["failed"])
    config.save_notifications(None, ["failed", "success"])  # keep URLs
    assert config.has_notification_urls()
    config.save_notifications("", ["failed"])  # remove them
    assert not config.has_notification_urls()


def test_bad_url_rejected_without_echoing_it(config: Config) -> None:
    with pytest.raises(ConfigError, match="line 2") as caught:
        config.save_notifications("ntfys://example.invalid/t\nbogus://secret-token", ["failed"])
    assert "secret-token" not in str(caught.value)
