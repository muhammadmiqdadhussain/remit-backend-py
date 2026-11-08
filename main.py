import os
import re
import asyncio
import random
import httpx
import json
import time
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
import smtplib
from email.message import EmailMessage
import base64
from binascii import unhexlify
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO_AVAILABLE = True
except Exception:
    AESGCM = None  # type: ignore
    CRYPTO_AVAILABLE = False

app = FastAPI(title="Remittance Rates API")

# ============ RATE ALERTS CONFIGURATION ============
ALERTS_FILE = Path("/tmp/rate_alerts.json")
ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)

# Optional symmetric key for encrypting alerts at rest. Provide 64-hex chars (32 bytes) in ALERTS_KEY.
ALERTS_KEY_HEX = os.getenv("ALERTS_KEY")
ALERTS_KEY: bytes | None = None
if ALERTS_KEY_HEX:
    try:
        ALERTS_KEY = unhexlify(ALERTS_KEY_HEX.strip())
        if len(ALERTS_KEY) != 32:
            print("ALERTS_KEY must be 32 bytes (64 hex chars); encryption disabled")
            ALERTS_KEY = None
        elif not CRYPTO_AVAILABLE:
            print("cryptography not installed; ALERTS_KEY provided but crypto unavailable")
            ALERTS_KEY = None
    except Exception as e:
        print(f"Failed to parse ALERTS_KEY: {e}")
        ALERTS_KEY = None

class RateAlertRequest(BaseModel):
    email: str  # Will validate format on frontend
    fromCurrency: str
    toCurrency: str
    targetRate: Optional[float] = None
    timestamp: str

def load_alerts() -> Dict[str, List[Dict]]:
    """Load rate alerts from file."""
    if not ALERTS_FILE.exists():
        return {}

    try:
        with open(ALERTS_FILE, 'r') as f:
            raw = json.load(f)

        # If file contains encrypted payload, decrypt it
        if isinstance(raw, dict) and raw.get("_encrypted") and ALERTS_KEY:
            enc_b64 = raw.get("_encrypted")
            try:
                payload = base64.b64decode(enc_b64)
                # nonce (12) + ciphertext
                nonce = payload[:12]
                ct = payload[12:]
                aesgcm = AESGCM(ALERTS_KEY)
                decrypted = aesgcm.decrypt(nonce, ct, None)
                return json.loads(decrypted.decode('utf-8'))
            except Exception as e:
                print(f"Failed to decrypt alerts file: {e}")
                return {}

        # fallback: assume plaintext JSON alerts structure
        if isinstance(raw, dict):
            return raw
        return {}
    except Exception:
        return {}

def save_alerts(alerts: Dict[str, List[Dict]]) -> None:
    """Save rate alerts to file."""
    try:
        # If ALERTS_KEY is set, encrypt the serialized alerts and store under _encrypted
        if ALERTS_KEY:
            try:
                aesgcm = AESGCM(ALERTS_KEY)
                nonce = os.urandom(12)
                plaintext = json.dumps(alerts, separators=(',', ':')).encode('utf-8')
                ct = aesgcm.encrypt(nonce, plaintext, None)
                payload = nonce + ct
                enc_b64 = base64.b64encode(payload).decode('utf-8')
                with open(ALERTS_FILE, 'w') as f:
                    json.dump({'_encrypted': enc_b64}, f)
                return
            except Exception as e:
                print(f"Encryption failed, falling back to plaintext save: {e}")

        with open(ALERTS_FILE, 'w') as f:
            json.dump(alerts, f, indent=2)
    except Exception as e:
        print(f"Error saving alerts: {e}")


# ============ EMAIL (OPTIONAL) CONFIGURATION ============
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
FROM_EMAIL = os.getenv("FROM_EMAIL", "no-reply@remitmatrix.com")

def send_confirmation_email(to_email: str, corridor: str) -> None:
    """Send a minimal confirmation email. Avoid including sensitive details.
    This function is optional: if SMTP_* env vars are not set it will be a no-op.
    """
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        # SMTP not configured in this environment; do nothing
        print("SMTP not configured; skipping confirmation email")
        return

    subject = f"RemitMatrix: Alert created for {corridor}"
    body = (
        f"Your rate alert for {corridor} has been created.\n\n"
        "We will notify you when conditions are met.\n\n"
        "Note: for your privacy we avoid including full alert details in email messages."
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
            print(f"Sent confirmation email to {to_email} for {corridor}")
    except Exception as e:
        print(f"Failed to send confirmation email: {e}")

# ============ OUTBOUND REQUEST HARDENING ============
HTTP_TIMEOUT_SECONDS = 6.0
MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 0.6
MAX_OUTBOUND_CONCURRENCY = 2
PROVIDER_BACKOFF_SECONDS = 300

REQUEST_SEMAPHORE = asyncio.Semaphore(MAX_OUTBOUND_CONCURRENCY)
PROVIDER_BACKOFF_UNTIL: Dict[str, float] = {}

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


def is_provider_in_backoff(provider: str) -> bool:
    return time.time() < PROVIDER_BACKOFF_UNTIL.get(provider, 0.0)


def mark_provider_backoff(provider: str, seconds: int = PROVIDER_BACKOFF_SECONDS) -> None:
    PROVIDER_BACKOFF_UNTIL[provider] = time.time() + float(seconds)


async def request_with_retry(
    client: httpx.AsyncClient,
    provider: str,
    method: str,
    url: str,
    **kwargs: Any,
) -> Optional[httpx.Response]:
    if is_provider_in_backoff(provider):
        return None

    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("User-Agent", random.choice(USER_AGENTS))
    timeout = kwargs.pop("timeout", HTTP_TIMEOUT_SECONDS)

    for attempt in range(MAX_RETRIES):
        await asyncio.sleep(random.uniform(0.05, 0.25))
        try:
            async with REQUEST_SEMAPHORE:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    timeout=timeout,
                    **kwargs,
                )

            if response.status_code < 400:
                return response

            if response.status_code in (403, 429):
                mark_provider_backoff(provider)
                print(f"[{provider}] blocked with status {response.status_code}, backing off for {PROVIDER_BACKOFF_SECONDS}s")
                return None

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_BASE_DELAY_SECONDS * (2 ** attempt) + random.uniform(0.0, 0.4))
                continue
            print(f"[{provider}] HTTP {response.status_code} for {url}")
            return None
        except httpx.RequestError as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_BASE_DELAY_SECONDS * (2 ** attempt) + random.uniform(0.0, 0.4))
                continue
            print(f"[{provider}] Network Error: {e}")
            return None

    return None

# Allow your TypeScript frontend to call this API without CORS errors
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For production, replace "*" with your TS frontend's domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ COUNTRY DATA ============
# ISO2 -> (ISO3, ISO numeric, currency, Remitly URL slug)
COUNTRIES: Dict[str, tuple] = {
    'GB': ('GBR', 826, 'GBP', 'united-kingdom'),
    'AT': ('AUT', 40, 'EUR', 'austria'),
    'BE': ('BEL', 56, 'EUR', 'belgium'),
    'BG': ('BGR', 100, 'EUR', 'bulgaria'),
    'HR': ('HRV', 191, 'EUR', 'croatia'),
    'CY': ('CYP', 196, 'EUR', 'cyprus'),
    'EE': ('EST', 233, 'EUR', 'estonia'),
    'FI': ('FIN', 246, 'EUR', 'finland'),
    'FR': ('FRA', 250, 'EUR', 'france'),
    'DE': ('DEU', 276, 'EUR', 'germany'),
    'GR': ('GRC', 300, 'EUR', 'greece'),
    'IE': ('IRL', 372, 'EUR', 'ireland'),
    'IT': ('ITA', 380, 'EUR', 'italy'),
    'LV': ('LVA', 428, 'EUR', 'latvia'),
    'LT': ('LTU', 440, 'EUR', 'lithuania'),
    'LU': ('LUX', 442, 'EUR', 'luxembourg'),
    'MT': ('MLT', 470, 'EUR', 'malta'),
    'NL': ('NLD', 528, 'EUR', 'netherlands'),
    'PT': ('PRT', 620, 'EUR', 'portugal'),
    'SK': ('SVK', 703, 'EUR', 'slovakia'),
    'SI': ('SVN', 705, 'EUR', 'slovenia'),
    'ES': ('ESP', 724, 'EUR', 'spain'),
    'IN': ('IND', 356, 'INR', 'india'),
    'PK': ('PAK', 586, 'PKR', 'pakistan'),
    'BD': ('BGD', 50, 'BDT', 'bangladesh'),
    'LK': ('LKA', 144, 'LKR', 'sri-lanka'),
    'NP': ('NPL', 524, 'NPR', 'nepal'),
    'NG': ('NGA', 566, 'NGN', 'nigeria'),
    'PH': ('PHL', 608, 'PHP', 'philippines'),
    'VN': ('VNM', 704, 'VND', 'vietnam'),
    'TH': ('THA', 764, 'THB', 'thailand'),
    'GH': ('GHA', 288, 'GHS', 'ghana'),
    'KE': ('KEN', 404, 'KES', 'kenya'),
    'MA': ('MAR', 504, 'MAD', 'morocco'),
}


def quote(provider: str, rate: float, fee: float, amount: float, **extra: Any) -> Dict[str, Any]:
    """Standard quote shape returned to the frontend. Regular (non-promo) values
    always drive ranking; promo fields describe a separate first-transfer offer."""
    result: Dict[str, Any] = {
        "provider": provider,
        "rate": round(rate, 6),
        "fee": round(fee, 2),
        # Fees are charged in the sending currency, so they reduce the amount converted.
        "received_amount": round((amount - fee) * rate, 2),
    }
    result.update({k: v for k, v in extra.items() if v is not None})
    return result


# ============ PROVIDERS ============
TAPTAP_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}


async def fetch_taptap_send(from_country: str, to_country: str, amount: float) -> Optional[Dict[str, Any]]:
    """Taptap Send publishes one JSON feed with every corridor's rate and fee tiers."""
    provider = "Taptap Send"
    try:
        if TAPTAP_CACHE["data"] is None or time.time() - TAPTAP_CACHE["at"] > CACHE_DURATION_SECONDS:
            async with httpx.AsyncClient() as client:
                response = await request_with_retry(
                    client, provider, "GET", "https://api.taptapsend.com/api/fxRates",
                    headers={'Appian-Version': 'web/2022-05-03.0', 'X-Device-Id': 'web', 'X-Device-Model': 'web'},
                )
            if response is None:
                return None
            TAPTAP_CACHE.update(at=time.time(), data=response.json())

        for country in TAPTAP_CACHE["data"].get('availableCountries', []):
            if country.get('isoCountryCode') != from_country:
                continue
            for corridor in country.get('corridors', []):
                if corridor.get('isoCountryCode') != to_country:
                    continue
                rate = float(corridor['fxRate'])
                # Fee tiers are keyed by minimum send amount; use the highest tier we reach.
                tiers = (corridor.get('feeSchedule') or {}).get('tiers') or []
                eligible = [t for t in tiers if float(t.get('minValue', 0)) <= amount]
                fee = float(max(eligible, key=lambda t: float(t['minValue']))['fee']) if eligible else 0.0
                return quote(provider, rate, fee, amount)
    except Exception as e:
        print(f"[{provider}] Error: {e}")
    return None


async def fetch_remit_choice(from_country: str, to_country: str, amount: float) -> Optional[Dict[str, Any]]:
    """RemitChoice calculator endpoint (HTML fragment)."""
    if from_country not in COUNTRIES or to_country not in COUNTRIES:
        return None
    src, dst = COUNTRIES[from_country], COUNTRIES[to_country]
    payload = {
        'sending_country': src[0],
        'receiving_country': dst[0],
        'sendingcurrencycode': src[2],
        'receivingcurrencycode': dst[2],
        'payout_method': 'Bank',
        'payout_patner': 'Bank',
        'sending_amount': str(amount),
        'receiving_amount': '',
        'total_payable_amount': '',
        'action': 's_country',
    }
    headers = {
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Referer': 'https://www.remitchoice.com/',
    }
    provider = "RemitChoice"
    async with httpx.AsyncClient() as client:
        try:
            response = await request_with_retry(client, provider, "POST", "https://www.remitchoice.com/ajaxcalculator.php", data=payload, headers=headers)
            if response is None:
                return None
            html = response.text
            rate_match = re.search(r'Exchange Rate 1 [A-Z]{3} = (\d+(?:\.\d+)?) [A-Z]{3}', html)
            fee_match = re.search(r'Transfer Fee: (\d+(?:\.\d+)?) [A-Z]{3}', html)
            if not rate_match:
                return None
            text = re.sub(r'<[^>]+>', ' ', html)
            methods = [label for key, label in (("Account Transfer", "Bank deposit"), ("Wallet", "Mobile wallet"), ("Cash Pickup", "Cash pickup")) if key in text]
            return quote(provider, float(rate_match.group(1)), float(fee_match.group(1)) if fee_match else 0.0, amount,
                         payout_methods=methods or None)
        except Exception as e:
            print(f"[{provider}] Error: {e}")
    return None


async def fetch_remitly(from_country: str, to_country: str, amount: float) -> Optional[Dict[str, Any]]:
    """Remitly corridor pages embed 'merchandisingFacts' with everyday and
    first-transfer (effective) rates plus promo metadata."""
    if to_country not in COUNTRIES:
        return None
    url = f"https://www.remitly.com/{from_country.lower()}/en/{COUNTRIES[to_country][3]}"
    headers = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    provider = "Remitly"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await request_with_retry(client, provider, "GET", url, headers=headers)
            if response is None:
                return None
            html = response.text
            # Remitly redirects unsupported corridors to a generic page; make sure it is the right one.
            if COUNTRIES[to_country][3] not in str(response.url):
                print(f"[{provider}] {from_country}->{to_country}: redirected to {response.url}, corridor not offered")
                return None

            facts_match = re.search(r'"merchandisingFacts":(\{[^{}]*\})', html)
            if not facts_match:
                print(f"[{provider}] {from_country}->{to_country}: no merchandisingFacts in page")
                return None
            facts = json.loads(facts_match.group(1))
            everyday = float(facts.get("everydayRateAsLowAs") or 0)
            if everyday <= 0:
                return None
            fee = float(facts.get("everydayFeeAsLowAs") or facts.get("feesAsLowAs") or 0)

            extra: Dict[str, Any] = {}
            methods = facts.get("disbursementMessaging")
            if methods:
                parts = [m.strip() for m in re.split(r',\s*(?:and\s+)?|\s+and\s+', methods) if m.strip()]
                extra["payout_methods"] = [m.upper() if m.lower() == "upi" else m[0].upper() + m[1:] for m in parts]

            promo_rate = float(facts.get("effectiveRateAsHighAs") or 0)
            if promo_rate > everyday:
                cap_match = re.search(r'promo_code=[A-Z0-9-]*?-(\d+)CAP', html)
                cap = float(cap_match.group(1)) if cap_match else None
                promo_fee = float(facts.get("effectiveFeeAsLowAs") or 0)
                promo_part = min(amount, cap) if cap else amount
                promo_received = (promo_part - promo_fee) * promo_rate + (amount - promo_part) * everyday
                message = re.search(r'"promoPackageMessage":"([^"]*)"', html)
                extra.update(
                    promotional_rate=round(promo_rate, 6),
                    promo_received_amount=round(promo_received, 2),
                    promo_max_amount=cap,
                    promo_description=(message.group(1).split("\\n")[0].strip() if message else "Special rate on your first transfer"),
                )
            return quote(provider, everyday, fee, amount, **extra)
        except Exception as e:
            print(f"[{provider}] Error: {e}")
    return None


async def _profee_calc(client: httpx.AsyncClient, src: tuple, dst: tuple, amount: float) -> Optional[Dict[str, Any]]:
    response = await request_with_retry(
        client, "Profee", "POST", "https://terminal.profee.com/api/v2/transfer/terminal/calculation",
        json={"from": {"currency": src[2], "amount": amount, "country": src[1]},
              "to": {"currency": dst[2], "amount": None, "country": dst[1]},
              "skipLimitValidation": True},
        headers={'Origin': 'https://www.profee.com', 'Referer': 'https://www.profee.com/'},
    )
    if response is None:
        return None
    data = response.json()
    if (data.get("status") or {}).get("code") != "OK":
        return None
    return data.get("body")


async def fetch_profee(from_country: str, to_country: str, amount: float) -> Optional[Dict[str, Any]]:
    """Profee's public calculator. A rate of type PROMO is a first-transfer rate
    valid up to maxAmount; quoting just above that cap returns the regular rate."""
    if from_country not in COUNTRIES or to_country not in COUNTRIES:
        return None
    src, dst = COUNTRIES[from_country], COUNTRIES[to_country]
    provider = "Profee"
    async with httpx.AsyncClient() as client:
        try:
            body = await _profee_calc(client, src, dst, amount)
            if not body:
                return None
            rate_info = body.get("currencyRate") or {}
            rate = float(rate_info.get("rate") or 0)
            if rate <= 0:
                return None
            fee = float((body.get("feeSum") or {}).get("amount") or 0)
            fee_is_promo = (body.get("feeSum") or {}).get("type") == "PROMO"

            if rate_info.get("type") != "PROMO":
                return quote(provider, rate, fee, amount,
                             fee_note="Introductory fee shown; later transfers may include a fee." if fee_is_promo else None)

            cap = float(((rate_info.get("maxAmount") or {}).get("amount")) or amount)
            regular_body = await _profee_calc(client, src, dst, max(amount, cap) + 1)
            regular_rate = float(((regular_body or {}).get("currencyRate") or {}).get("rate") or 0)
            if regular_rate <= 0 or regular_rate >= rate:
                return None
            promo_part = min(amount, cap)
            promo_received = (promo_part - fee) * rate + (amount - promo_part) * regular_rate
            return quote(
                provider, regular_rate, fee, amount,
                promotional_rate=round(rate, 6),
                promo_received_amount=round(promo_received, 2),
                promo_max_amount=cap,
                promo_description="Promotional exchange rate on your first transfer",
                fee_note="Introductory fee shown; later transfers may include a fee." if fee_is_promo else None,
            )
        except Exception as e:
            print(f"[{provider}] Error: {e}")
    return None


# ============ RATES ENDPOINT ============
RATE_CACHE: Dict[str, Any] = {}
CACHE_DURATION_SECONDS = 600  # 10 minutes


@app.get("/api/rates")
@app.get("/rates")
async def get_rates(
    from_country: str = Query(..., description="ISO 2 code of the sending country (e.g. GB, DE)"),
    to_country: str = Query(..., description="ISO 2 code of the receiving country (e.g. IN, PK)"),
    amount: float = Query(100.0, gt=0, le=1_000_000, description="Amount to send, in the sending currency"),
):
    from_country = from_country.upper()
    to_country = to_country.upper()
    cache_key = f"{from_country}_{to_country}_{amount}"
    now = time.time()

    cached = RATE_CACHE.get(cache_key)
    if cached and now - cached[0] < CACHE_DURATION_SECONDS:
        return cached[1]

    results = await asyncio.gather(
        fetch_taptap_send(from_country, to_country, amount),
        fetch_remit_choice(from_country, to_country, amount),
        fetch_remitly(from_country, to_country, amount),
        fetch_profee(from_country, to_country, amount),
    )
    valid = [r for r in results if r and r.get("received_amount", 0) > 0]

    # Gracefully degrade to a stale cache if every provider fails temporarily.
    if not valid and cached and now - cached[0] < 3600 and cached[1]:
        return cached[1]

    valid.sort(key=lambda r: r["received_amount"], reverse=True)
    RATE_CACHE[cache_key] = (now, valid)
    return valid


# ============ MID-MARKET RATE ============
MID_MARKET_CACHE: Dict[str, Any] = {}
MID_MARKET_TTL_SECONDS = 3600


@app.get("/api/mid-market")
async def get_mid_market(
    base: str = Query(..., min_length=3, max_length=3),
    quote_currency: str = Query(..., alias="quote", min_length=3, max_length=3),
):
    """Reference mid-market rate from ExchangeRate-API's open feed (updated daily),
    with the fawazahmed0 currency-api as a fallback."""
    base, quote_currency = base.upper(), quote_currency.upper()
    cached = MID_MARKET_CACHE.get(base)
    if not cached or time.time() - cached["fetched"] > MID_MARKET_TTL_SECONDS:
        entry = None
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(f"https://open.er-api.com/v6/latest/{base}", timeout=HTTP_TIMEOUT_SECONDS)
                d = r.json()
                if d.get("result") == "success":
                    entry = {"rates": d["rates"], "updated": d.get("time_last_update_unix"), "source": "ExchangeRate-API",
                             "source_url": "https://www.exchangerate-api.com"}
            except Exception as e:
                print(f"[mid-market] primary failed: {e}")
            if entry is None:
                try:
                    r = await client.get(f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base.lower()}.json", timeout=HTTP_TIMEOUT_SECONDS)
                    d = r.json()
                    entry = {"rates": {k.upper(): v for k, v in d[base.lower()].items()},
                             "updated": int(datetime.strptime(d["date"], "%Y-%m-%d").timestamp()),
                             "source": "currency-api", "source_url": "https://github.com/fawazahmed0/exchange-api"}
                except Exception as e:
                    print(f"[mid-market] fallback failed: {e}")
        if entry is None:
            if not cached:
                raise HTTPException(status_code=503, detail="Mid-market rate unavailable")
        else:
            entry["fetched"] = time.time()
            MID_MARKET_CACHE[base] = cached = entry

    rate = cached["rates"].get(quote_currency)
    if not rate:
        raise HTTPException(status_code=404, detail="Currency not supported")
    return {"base": base, "quote": quote_currency, "rate": rate, "updated": cached["updated"],
            "source": cached["source"], "source_url": cached["source_url"]}


# ============ RATE ALERTS ENDPOINTS ============

@app.post("/api/rate-alerts")
async def create_rate_alert(request: RateAlertRequest):
    """
    Create a new rate alert for a specific currency pair.
    Prevents duplicate alerts for the same email + corridor.
    """
    # Validate email format
    if not request.email or "@" not in request.email:
        raise HTTPException(
            status_code=400, 
            detail={"message": "Please enter a valid email address"}
        )
    
    # Validate currency codes
    if not request.fromCurrency or len(request.fromCurrency) != 2:
        raise HTTPException(
            status_code=400, 
            detail={"message": "Invalid source currency code"}
        )
    
    if not request.toCurrency or len(request.toCurrency) != 2:
        raise HTTPException(
            status_code=400, 
            detail={"message": "Invalid destination currency code"}
        )
    
    # Prevent self-transfer
    if request.fromCurrency.upper() == request.toCurrency.upper():
        raise HTTPException(
            status_code=400, 
            detail={"message": "Source and destination currencies must be different"}
        )
    
    # Load existing alerts
    alerts = load_alerts()
    
    # Create corridor key
    corridor_key = f"{request.fromCurrency.upper()}-{request.toCurrency.upper()}"
    
    # Check for existing alert from this email for this corridor
    if corridor_key not in alerts:
        alerts[corridor_key] = []
    
    # Check if user already has an alert for this corridor
    existing_alert = any(
        alert["email"].lower() == request.email.lower() 
        for alert in alerts[corridor_key]
    )
    
    if existing_alert:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "You already have an alert for this corridor. Check your inbox for updates."
            }
        )
    
    # Add new alert
    alert_entry = {
        "email": request.email,
        "targetRate": request.targetRate,
        "timestamp": request.timestamp,
        "active": True,
    }
    
    alerts[corridor_key].append(alert_entry)
    save_alerts(alerts)
    # Try to send a minimal confirmation email (optional)
    try:
        send_confirmation_email(request.email, corridor_key)
    except Exception:
        # swallow email failures so API remains functional
        pass

    return {
        "success": True,
        "message": f"Rate alert created for {corridor_key}",
        "corridor": corridor_key,
        "email": request.email
    }


@app.get("/api/rate-alerts/status")
async def get_alert_status(email: str = Query(...)):
    """Get the status of rate alerts for a specific email."""
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")
    
    alerts = load_alerts()
    user_alerts = {}
    
    for corridor, corridor_alerts in alerts.items():
        user_corridor_alerts = [
            alert for alert in corridor_alerts 
            if alert["email"].lower() == email.lower()
        ]
        if user_corridor_alerts:
            user_alerts[corridor] = user_corridor_alerts
    
    return {
        "email": email,
        "alerts": user_alerts,
        "count": sum(len(v) for v in user_alerts.values())
    }


@app.delete("/api/rate-alerts/{corridor}")
async def delete_rate_alert(corridor: str, email: str = Query(...)):
    """Delete a rate alert."""
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")
    
    alerts = load_alerts()
    
    if corridor not in alerts:
        raise HTTPException(status_code=404, detail="Corridor not found")
    
    # Remove alert for this user
    initial_count = len(alerts[corridor])
    alerts[corridor] = [
        alert for alert in alerts[corridor]
        if alert["email"].lower() != email.lower()
    ]
    
    if len(alerts[corridor]) == initial_count:
        raise HTTPException(
            status_code=404, 
            detail="Alert not found for this user"
        )
    
    # Remove corridor if no alerts left
    if not alerts[corridor]:
        del alerts[corridor]
    
    save_alerts(alerts)
    
    return {"success": True, "message": f"Alert deleted for {corridor}"}
