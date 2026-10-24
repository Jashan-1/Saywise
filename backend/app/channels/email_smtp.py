import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from app.core.config import Settings

FOOTER = "\n\n-- \nSent by SaySo for your team. Open the link to approve, decline or counter."


class SmtpEmail:
    """Plain SMTP (D24): works with Google Workspace, Microsoft 365 or any mail server."""

    def __init__(self, settings: Settings) -> None:
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.security = settings.smtp_security
        self.username = settings.smtp_username
        self.password = settings.smtp_password.get_secret_value()
        self.sender = settings.smtp_from

    async def send(self, to: str, text: str) -> str:
        """The first line is the subject; the whole text is the body."""
        if not self.host or not self.sender:
            raise RuntimeError("SMTP is not configured (SAYSO_SMTP_HOST, SAYSO_SMTP_FROM)")
        msg = EmailMessage()
        msg["From"], msg["To"] = self.sender, to
        msg["Subject"] = text.splitlines()[0][:150] if text else "SaySo"
        msg["Message-ID"] = make_msgid(domain=self.sender.rpartition("@")[2] or None)
        msg.set_content(text + FOOTER)
        await asyncio.to_thread(self._deliver, msg)  # smtplib blocks
        return str(msg["Message-ID"])

    def _deliver(self, msg: EmailMessage) -> None:
        tls = ssl.create_default_context()
        smtp = (
            smtplib.SMTP_SSL(self.host, self.port, timeout=10, context=tls)
            if self.security == "ssl"
            else smtplib.SMTP(self.host, self.port, timeout=10)
        )
        with smtp:
            if self.security == "starttls":
                smtp.starttls(context=tls)
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(msg)
