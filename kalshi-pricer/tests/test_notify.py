from __future__ import annotations

from pathlib import Path

from src.notify import TelegramKillListener, TelegramNotifier


class _FakeResponse:
    status_code = 200
    text = "ok"


class _FakeClient:
    def __init__(self) -> None:
        self.posts: list[dict] = []

    def post(self, url: str, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return _FakeResponse()

    def close(self) -> None:
        pass


def test_notifier_sends_to_admin_static_alerts_and_subscribers(monkeypatch, tmp_path):
    fake = _FakeClient()
    monkeypatch.setattr("src.notify.httpx.Client", lambda timeout: fake)
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_IDS", "222, 333,222")
    subscriber_path = tmp_path / "subscribers"
    subscriber_path.write_text("333\n444\n")

    notifier = TelegramNotifier(
        token="token",
        chat_id="111",
        subscriber_path=subscriber_path,
    )

    notifier.send("hello")

    assert [post["json"]["chat_id"] for post in fake.posts] == [
        "111",
        "222",
        "333",
        "444",
    ]


def test_foreign_start_can_subscribe_without_admin_commands(tmp_path: Path):
    fake = _FakeClient()
    subscriber_path = tmp_path / "subscribers"
    kill_file = tmp_path / ".kill"
    listener = TelegramKillListener(
        kill_file=kill_file,
        token="token",
        chat_id="admin",
        allow_alert_subscribe=True,
        subscriber_path=subscriber_path,
    )
    listener._http = fake

    listener._handle({"message": {"chat": {"id": "friend"}, "text": "/start"}})
    listener._handle({"message": {"chat": {"id": "friend"}, "text": "/kill"}})

    assert subscriber_path.read_text().splitlines() == ["friend"]
    assert kill_file.exists() is False
    assert fake.posts[0]["json"]["chat_id"] == "friend"
