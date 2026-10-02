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


def test_named_notifications(tmp_path: Path) -> None:
    config = Config(State(tmp_path / "state.db"), SecretBox(b"k" * 32), lambda: NOW)
    first = config.add_notification_target("Phone", "ntfys://ntfy.sh/topic-one")
    config.add_notification_target("Home Assistant", "hassio://ha.local:8123/abc.def.ghi")
    listed = config.notification_targets()
    assert [(t.name, t.service) for t in listed] == [
        ("Phone", "ntfy"),
        ("Home Assistant", "HomeAssistant"),
    ]
    assert config.remove_notification_target(first)
    assert [t.name for t in config.notification_targets()] == ["Home Assistant"]
    with pytest.raises(ConfigError, match="hassio://"):
        config.add_notification_target("Web", "https://ha.local:8123/api")
    with pytest.raises(ConfigError, match="short name"):
        config.add_notification_target(" ", "ntfys://ntfy.sh/t")


def test_saved_urls_from_before_names_are_kept_and_named(tmp_path: Path) -> None:
    box = SecretBox(b"k" * 32)
    state = State(tmp_path / "state.db")
    urls = "ntfys://ntfy.sh/a\nntfys://ntfy.sh/b\nhassio://ha.local:8123/abc.def.ghi"
    state.set_sealed("notify.urls", box.seal("notify.urls", urls.encode()), NOW)
    config = Config(state, box, lambda: NOW)
    names = [t.name for t in config.notification_targets()]
    assert names == ["ntfy", "ntfy 2", "HomeAssistant"]
    assert state.get_sealed("notify.urls") is None
    assert config.notifier().configured


def test_failed_delivery_gives_the_services_reason() -> None:
    from googich_takeaway.notify import deliver

    ok, reasons = deliver(["json://127.0.0.1:9/"], Message(Outcome.SUCCESS, "Test"))
    assert ok is False
    assert reasons


def test_home_assistant_test_says_where_it_went() -> None:
    from googich_takeaway.notify import delivery_note

    assert "mobile_app_pixel" in delivery_note("hassio://ha.local:8123/a.b.c/mobile_app_pixel")
    assert "mobile_app_pixel" in delivery_note("hassio://ha.local:8123/a.b.c?to=mobile_app_pixel")
    assert "the bell" in delivery_note("hassio://ha.local:8123/a.b.c")
    assert delivery_note("ntfys://ntfy.sh/topic") == ""
