import email
from typing import Any

import pytest
from pydantic import SecretStr

from app.channels import email_smtp
from app.channels.email_smtp import SmtpEmail
from app.core.config import get_settings


class FakeSmtp:
    calls: list[tuple[str, Any]] = []

    def __init__(self, host: str, port: int, timeout: int, context: Any = None) -> None:
        self.calls.append(("connect", (host, port, type(self).__name__)))

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def starttls(self, context: Any) -> None:
        self.calls.append(("starttls", None))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user))

    def send_message(self, msg: email.message.EmailMessage) -> None:
        self.calls.append(("send", msg))


class FakeSmtpSsl(FakeSmtp):
    pass


@pytest.fixture
def smtp(monkeypatch) -> SmtpEmail:
    FakeSmtp.calls = []
    monkeypatch.setattr(email_smtp.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(email_smtp.smtplib, "SMTP_SSL", FakeSmtpSsl)
    s = get_settings().model_copy(
        update={
            "smtp_host": "smtp.example",
            "smtp_from": "SaySo <sayso@dealer.example>",
            "smtp_username": "sayso",
            "smtp_password": SecretStr("pw"),
        }
    )
    return SmtpEmail(s)


async def test_sends_over_starttls_with_subject_from_first_line(smtp: SmtpEmail) -> None:
    message_id = await smtp.send("raj@dealer.example", "SaySo #K7Z - Fremont\nbody line\nhttps://x")
    kinds = [kind for kind, _ in FakeSmtp.calls]
    assert kinds == ["connect", "starttls", "login", "send"]
    msg = FakeSmtp.calls[-1][1]
    assert (msg["To"], msg["Subject"], msg["Message-ID"]) == (
        "raj@dealer.example",
        "SaySo #K7Z - Fremont",
        message_id,
    )
    assert "body line" in msg.get_content() and "Sent by SaySo" in msg.get_content()


async def test_ssl_and_no_login(smtp: SmtpEmail) -> None:
    smtp.security, smtp.username = "ssl", ""
    await smtp.send("a@b.example", "hello")
    assert FakeSmtp.calls[0][1][2] == "FakeSmtpSsl"
    assert [kind for kind, _ in FakeSmtp.calls] == ["connect", "send"]


async def test_unconfigured_smtp_refuses(smtp: SmtpEmail) -> None:
    smtp.host = ""
    with pytest.raises(RuntimeError, match="not configured"):
        await smtp.send("a@b.example", "hello")
