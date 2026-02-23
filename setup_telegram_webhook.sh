#!/bin/bash
# Reads TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, and NGROK_URL from .env
# and registers the Telegram webhook.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "❌ .env file not found at $ENV_FILE"
  exit 1
fi

# Load .env (skip comments and empty lines)
while IFS= read -r line || [[ -n "$line" ]]; do
  # Skip empty lines and lines starting with #
  if [[ -n "$line" ]] && [[ ! "$line" =~ ^[[:space:]]*# ]]; then
    export "$line"
  fi
done < "$ENV_FILE"

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is not set in .env}"
: "${TELEGRAM_WEBHOOK_SECRET:?TELEGRAM_WEBHOOK_SECRET is not set in .env}"
: "${NGROK_URL:?NGROK_URL is not set in .env (e.g. https://xxxx.ngrok-free.app)}"

WEBHOOK_URL="${NGROK_URL%/}/webhook/telegram"

echo "📡 Registering webhook..."
echo "   URL:    $WEBHOOK_URL"
echo "   Secret: ${TELEGRAM_WEBHOOK_SECRET:0:6}***"

response=$(curl -s -X POST \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"${WEBHOOK_URL}\", \"secret_token\": \"${TELEGRAM_WEBHOOK_SECRET}\"}")

echo ""
echo "✅ Telegram response:"
echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
