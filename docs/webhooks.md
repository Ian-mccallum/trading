# TradingView webhook integration

Endpoint: `POST /webhooks/tradingview` (Content-Type: application/json).

## Alert message template

In a TradingView alert, set the webhook URL to
`https://<your-host>/webhooks/tradingview` and the message to:

```json
{
  "secret": "<TRADINGVIEW_WEBHOOK_SECRET>",
  "symbol": "{{ticker}}",
  "action": "buy",
  "qty": 1,
  "price": {{close}},
  "time": "{{timenow}}",
  "signal_id": "{{ticker}}-{{interval}}-{{timenow}}",
  "strategy": "sma_cross",
  "comment": "optional note"
}
```

| Field | Required | Notes |
|---|---|---|
| `secret` | yes | must equal `TRADINGVIEW_WEBHOOK_SECRET`; compared constant-time; stored/logged as `[redacted]` |
| `symbol` | yes | `NASDAQ:AAPL` → normalized to `AAPL` |
| `action` | yes | `buy` \| `sell` \| `close` |
| `qty` | no | omitted → sized by the platform (≤ `RISK_MAX_ORDER_NOTIONAL`, ≤ 5% of equity, whole shares) |
| `time` | no | ISO timestamp; alerts older than `WEBHOOK_MAX_SIGNAL_AGE_SECONDS` are rejected as expired |
| `signal_id` | no | dedupe key; omitted → content hash per minute bucket |
| `strategy` | no | attributes the decision to an approved strategy version |

Extra fields are preserved in the stored payload.

## Responses

| HTTP | `status` | Meaning |
|---|---|---|
| 202 | `validated` | stored and queued for processing |
| 200 | `duplicate` | same `signal_id`/content already seen — safe for TradingView retries |
| 401 | `authentication failed` | wrong secret / bad HMAC (stored, redacted, for forensics) |
| 422 | `invalid payload` / `signal expired` | schema failure (not stored) / too old (stored) |
| 503 | `webhook not configured` | no secret configured server-side; endpoint refuses everything |

## What happens next

Validated signals are queued (idempotent job id per signal) to the worker:
market context (features + regime) is attached, quantity sized if absent,
and the resulting `TradeIntent` goes through the **full risk engine** before
any broker call — a webhook cannot bypass any limit, the kill switch, or the
live gates. The final decision and its risk verdict are queryable via
`GET /admin/decisions`.

## Hardening options

- `WEBHOOK_HMAC_SECRET`: additionally require an `X-Signature` header =
  hex HMAC-SHA256 of the raw body (for header-capable senders/proxies;
  TradingView itself cannot set headers).
- Terminate TLS in front (TradingView requires HTTPS on port 443).
- Keep `RISK_SYMBOL_ALLOWLIST` tight so even a leaked secret can only trade
  the symbols you allow, at the sizes the risk engine allows.
