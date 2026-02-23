import logging
import os
import httpx

log = logging.getLogger("telegram_client")


async def send_telegram_message(to_chat_id: str, text: str) -> bool:
    """Send an outbound Telegram message via the Bot API, or log it when credentials are absent.

    ``to_chat_id`` may be a plain integer ID (e.g. ``123456``) or a session ID
    in ``telegram:{chat_id}`` format — both are handled correctly.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")

    # Normalize chat_id: strip any "telegram:" prefix.
    chat_id = to_chat_id.strip().replace("telegram:", "", 1)

    if token:
        url = "https://api.telegram.org/bot{}/sendMessage".format(token)
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    url,
                    json={"chat_id": chat_id, "text": text},
                )
                response.raise_for_status()
                log.info("Sent Telegram message to %s", chat_id)
                return True
            except Exception as e:
                log.error("Failed to send Telegram message to %s: %s", chat_id, e)
                return False
    else:
        log.info("MOCK: Telegram message to %s: %s", chat_id, text)
        return True
