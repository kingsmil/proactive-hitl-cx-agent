import logging
import os
import httpx

log = logging.getLogger("whatsapp_client")

async def send_whatsapp_message(to_phone: str, text: str) -> bool:
    """
    Sends an outbound WhatsApp message.
    In a real implementation, this would call the Twilio or WhatsApp Business API.
    For this mock implementation, we just log it unless specific environment
    variables are set to connect to a real API.
    """
    from_phone = os.environ.get("TWILIO_PHONE_NUMBER", "+15550000000")
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    
    # Strip any "whatsapp:" prefix for cleanliness if needed, 
    # but Twilio often expects it. We'll ensure it's there.
    if not to_phone.startswith("whatsapp:"):
        if to_phone.startswith("+"):
             to_phone = f"whatsapp:{to_phone}"
    
    if not from_phone.startswith("whatsapp:"):
         from_phone = f"whatsapp:{from_phone}"

    if account_sid and auth_token:
        # Mock actual HTTP request, not executed unless env vars are present
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    url,
                    auth=(account_sid, auth_token),
                    data={
                        "From": from_phone,
                        "To": to_phone,
                        "Body": text
                    }
                )
                response.raise_for_status()
                log.info("Successfully sent real WhatsApp message to %s", to_phone)
                return True
            except Exception as e:
                log.error("Failed to send WhatsApp message to %s: %s", to_phone, str(e))
                return False
    else:
        # Mock behavior
        log.info("MOCK: Sent WhatsApp message to %s: %s", to_phone, text)
        return True
