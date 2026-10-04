import json
import os
import urllib.error
import urllib.request


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
    """Send the printed-receipt details on WhatsApp; without Meta keys it only prints the message (dry run)."""
    to = _normalize_phone(phone)
    if not to:
        print(f"WhatsApp skipped for {receipt['name']}: phone number is missing or not a valid Indian number.")
        return

    # Order must match the {{1}}..{{8}} variables of the approved Meta template.
    fields = [
        receipt["name"], receipt["event"], receipt["event_date"], receipt["host"], receipt["serial"],
        _format_amount(receipt["amount"]), receipt["staff"], receipt["time"],
    ]
    fields = [str(value) if value not in (None, "") else "-" for value in fields]
    token = os.getenv("WHATSAPP_TOKEN")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

    if not token or not phone_number_id:
        text = (
            f"Thanks for coming to our {fields[1]}, {fields[0]}.\n"
            f"Receipt No: {fields[4]}\nEvent: {fields[1]} ({fields[2]})\nHost: {fields[3]}\n"
            f"Contribution received: Rs.{fields[5]}\nReceived by: {fields[6]} on {fields[7]}"
        )
        print(f"[WhatsApp dry run] to ...{to[-4:]}:\n{text}")
        return

    version = os.getenv("WHATSAPP_API_VERSION", "v21.0")
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": os.getenv("WHATSAPP_TEMPLATE_NAME", "contribution_receipt"),
            "language": {"code": os.getenv("WHATSAPP_TEMPLATE_LANG", "en")},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": value} for value in fields],
                }
            ],
        },
    }
    request = urllib.request.Request(
        f"https://graph.facebook.com/{version}/{phone_number_id}/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print(f"WhatsApp sent to ...{to[-4:]} (HTTP {response.status}).")
    except urllib.error.HTTPError as error:
        print(f"WhatsApp failed for ...{to[-4:]}: HTTP {error.code} {error.read().decode('utf-8', 'replace')[:300]}")
    except Exception as error:
        print(f"WhatsApp failed for ...{to[-4:]}: {error}")
