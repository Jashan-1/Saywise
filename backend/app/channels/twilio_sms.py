import httpx

from app.core.config import Settings

API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


class TwilioSms:
    def __init__(self, settings: Settings) -> None:
        self.sid = settings.twilio_account_sid
        self.token = settings.twilio_auth_token.get_secret_value()
        self.sender = settings.twilio_from_number

    async def send(self, to: str, text: str) -> str:
        # shortcut: one HTTP client per message; share a client if SMS volume makes it matter.
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                API.format(sid=self.sid),
                data={"To": to, "From": self.sender, "Body": text},
                auth=(self.sid, self.token),
            )
        r.raise_for_status()
        return str(r.json()["sid"])
