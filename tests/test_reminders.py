from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from googich_takeaway import reminders
from googich_takeaway.config import Config, ConfigError
from googich_takeaway.credentials import SecretBox
from googich_takeaway.notify import Message, Notifier, Outcome
from googich_takeaway.state import DownloadRecord, State

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    state = State(tmp_path / "state.db")
    folder = tmp_path / "manual"
    folder.mkdir()
    made = Config(state, SecretBox(b"k" * 32), lambda: NOW)
    made.add_local_source("Manual", str(folder))
    return made


def _downloaded(config: Config, at: datetime) -> None:
    config.state.record_download(
        DownloadRecord(
            source="local:/x",
            file_id=f"f-{at.isoformat()}",
            fingerprint="1",
            name="takeout-20260101T000000Z-001.zip",
            size=1,
            link=None,
            local_path="/x",
            downloaded_at=at,
            forgotten_at=None,
            removed_at=None,
        )
    )


def test_no_reminder_before_any_export(config: Config) -> None:
    assert reminders.takeout_reminders(config, config.state, NOW) == []


def test_no_new_export_for_70_days(config: Config) -> None:
    _downloaded(config, NOW - timedelta(days=69))
    assert reminders.takeout_reminders(config, config.state, NOW) == []
    found = reminders.takeout_reminders(config, config.state, NOW + timedelta(days=2))
    assert [r.title for r in found] == ["No new Takeout export for a while"]
    assert "71 days ago" in found[0].lines[0]


def test_schedule_ending_soon(config: Config) -> None:
    config.save_takeout_schedule_started("2025-10-15")
    found = reminders.takeout_reminders(config, config.state, NOW)
    assert [r.title for r in found] == ["Your Takeout schedule ends soon"]
    assert "15 October 2026" in found[0].lines[0]
    after = NOW + timedelta(days=30)
    assert reminders.takeout_reminders(config, config.state, after) == []


def test_schedule_date_is_checked(config: Config) -> None:
    with pytest.raises(ConfigError, match="year-month-day"):
        config.save_takeout_schedule_started("15/10/2025")
    with pytest.raises(ConfigError, match="not possible"):
        config.save_takeout_schedule_started("2030-01-01")
    config.save_takeout_schedule_started("2026-01-31")
    assert config.takeout_schedule_started() == date(2026, 1, 31)
    config.save_takeout_schedule_started("")
    assert config.takeout_schedule_started() is None


def test_each_reminder_is_sent_once(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Message] = []

    def capture(notifier: Notifier, message: Message, force: bool = False) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(Notifier, "send", capture)
    _downloaded(config, NOW - timedelta(days=80))
    assert reminders.send_reminders(config, config.state, NOW) == 1
    assert reminders.send_reminders(config, config.state, NOW + timedelta(hours=1)) == 0
    assert [m.outcome for m in sent] == [Outcome.REMINDER]
    _downloaded(config, NOW)  # a new export arrived
    assert reminders.send_reminders(config, config.state, NOW) == 0
    later = NOW + timedelta(days=75)
    assert reminders.send_reminders(config, config.state, later) == 1  # stale again: new reminder
