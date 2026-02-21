import logging
import os
import httpx

log = logging.getLogger("whatsapp_client")


async def send_whatsapp_message(to_phone: str, text: str) -> bool:
    """Send an outbound WhatsApp message via Twilio, or log it when credentials are absent.

    ``to_phone`` may be a plain phone number (``+15551234567``) or a session ID
    in ``whatsapp:{phone}`` format — both are handled correctly.
    """
    from_phone = os.environ.get("TWILIO_PHONE_NUMBER", "+15550000000")
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")

    # Ensure both numbers carry the "whatsapp:" prefix expected by the Twilio API.
    if not to_phone.startswith("whatsapp:"):
        if to_phone.startswith("+"):
            to_phone = "whatsapp:{}".format(to_phone)

    if not from_phone.startswith("whatsapp:"):
        from_phone = "whatsapp:{}".format(from_phone)

    if account_sid and auth_token:
        url = "https://api.twilio.com/2010-04-01/Accounts/{}/Messages.json".format(account_sid)
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    url,
                    auth=(account_sid, auth_token),
                    data={"From": from_phone, "To": to_phone, "Body": text},
                )
                response.raise_for_status()
                log.info("Sent WhatsApp message to %s", to_phone)
                return True
            except Exception as e:
                log.error("Failed to send WhatsApp message to %s: %s", to_phone, e)
                return False
    else:
        log.info("MOCK: WhatsApp message to %s: %s", to_phone, text)
        return True
