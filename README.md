# Backend notes (rate alerts)

## Run locally

```sh
cd /var/www/remitmatrix
python3 -m venv .venv
.venv/bin/pip install fastapi httpx "uvicorn[standard]" cryptography
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

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
