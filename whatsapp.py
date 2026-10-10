import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request


def _load_env_file() -> None:
    """Local runs on SQL Server never load .env, so read it here; real environment variables (Render) win."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as env_file:
            for line in env_file:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_env_file()


def _normalize_phone(raw: str | None) -> str | None:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return None


def _format_amount(amount: float) -> str:
    return f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"


def send_contribution_message(phone: str | None, receipt: dict) -> None:
    """Send the receipt on WhatsApp through Twilio; without Twilio keys it only prints the message (dry run)."""
    to = _normalize_phone(phone)
    if not to:
        print(f"WhatsApp skipped for {receipt['name']}: phone number is missing or not a valid Indian number.")
        return

    name, event, event_date, host, serial, amount, staff, when = (
        str(value) if value not in (None, "") else "-"
        for value in (
            receipt["name"], receipt["event"], receipt["event_date"], receipt["host"], receipt["serial"],
            _format_amount(receipt["amount"]), receipt["staff"], receipt["time"],
        )
    )
    text = (
        "*Moi Sei*\n"
        f"Thank you, {name}, for coming to {event} on {event_date}.\n"
        f"Host: {host}\nReceipt No: {serial}\n"
        f"Contribution received: Rs.{amount}\nReceived by: {staff} on {when}.\nThank you!"
    )

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    sender = os.getenv("TWILIO_WHATSAPP_FROM")
    if not (account_sid and auth_token and sender):
        print(f"[WhatsApp dry run] to ...{to[-4:]}:\n{text}")
        return

    form = {"From": sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}", "To": f"whatsapp:+{to}"}
    content_sid = os.getenv("TWILIO_CONTENT_SID")
    if content_sid:
        # Production: an approved template; variables {{1}}..{{8}} follow the order below.
        values = (name, event, event_date, host, serial, amount, staff, when)
        form["ContentSid"] = content_sid
        form["ContentVariables"] = json.dumps({str(i): value for i, value in enumerate(values, start=1)}, ensure_ascii=False)
    else:
        # Sandbox / 24-hour window: plain text is allowed.
        form["Body"] = text

    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    request = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        data=urllib.parse.urlencode(form).encode(),
        headers={"Authorization": f"Basic {credentials}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
            print(f"WhatsApp accepted for ...{to[-4:]} (status {result.get('status')}, sid {result.get('sid')}).")
    except urllib.error.HTTPError as error:
        print(f"WhatsApp failed for ...{to[-4:]}: HTTP {error.code} {error.read().decode('utf-8', 'replace')[:300]}")
    except Exception as error:
        print(f"WhatsApp failed for ...{to[-4:]}: {error}")
