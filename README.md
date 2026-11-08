# RemitMatrix backend

## Run locally

```sh
cd /var/www/remitmatrix/backend
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/uvicorn main:app --host 127.0.0.1 --port 8005
```

In production the API runs as the `remitmatrix-backend` systemd service on
port 8005, and nginx proxies `/api/` to it.

## Rates API

- `GET /api/rates?from_country=GB&to_country=PK&amount=1000` returns quotes from
  Taptap Send, RemitChoice, Remitly and Profee, sorted by recipient payout
  (`(amount - fee) * rate`). Cached for 10 minutes. Each quote has `rate`,
  `fee`, `received_amount` and, where the provider publishes one, a
  first-transfer offer: `promotional_rate`, `promo_received_amount`,
  `promo_max_amount`, `promo_description`. `payout_methods` is included only
  when the provider's own data lists them (Remitly, RemitChoice).
- `GET /api/mid-market?base=EUR&quote=PKR` returns a reference mid-market rate
  from ExchangeRate-API's open feed (updated daily; fallback: currency-api),
  cached for 1 hour.

Not available from this server: ACE Money Transfer (Cloudflare block) and
MoneyGram (DataDome bot protection on its quote API).

## Rate alert API

- `POST /api/rate-alerts`
- `GET /api/rate-alerts/status?email=...`
- `DELETE /api/rate-alerts/{corridor}?email=...`

Alerts are stored in `/tmp/rate_alerts.json`.

## Security options

### At-rest encryption for alert storage (recommended)

Set `ALERTS_KEY` to a 64-hex-character key (32 bytes). When set, alert data is encrypted using AES-GCM before writing to disk.

```sh
export ALERTS_KEY=$(openssl rand -hex 32)
```

### Email confirmation (optional)

Set SMTP values to send minimal confirmation emails after alert creation:

```sh
export SMTP_HOST=smtp.example.com
export SMTP_PORT=587
export SMTP_USER=your-user
export SMTP_PASS=your-pass
export FROM_EMAIL=no-reply@remitmatrix.com
```

Email content is intentionally minimal and avoids sensitive alert payload details.
