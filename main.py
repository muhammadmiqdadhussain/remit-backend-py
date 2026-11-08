import os
import re
import asyncio
import random
import httpx
import json
import time
from pathlib import Path
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


async def fetch_taptap_send(from_country: str, to_country: str, amount: float) -> Optional[Dict[str, Any]]:
    """Native JSON API scraping for Taptap Send"""
    url = "https://api.taptapsend.com/api/fxRates"
    headers = {
        'Appian-Version': 'web/2022-05-03.0',
        'X-Device-Id': 'web',
        'X-Device-Model': 'web',
    }
    
    provider = "Taptap Send"
    async with httpx.AsyncClient() as client:
        try:
            response = await request_with_retry(client, provider, "GET", url, headers=headers)
            if response is None:
                return None
            data = response.json()
            
            for country in data.get('availableCountries', []):
                if country['isoCountryCode'] == from_country:
                    for corridor in country.get('corridors', []):
                        if corridor['isoCountryCode'] == to_country:
                            rate = float(corridor['fxRate'])
                            fee = float(corridor.get('fee') or 0.0)
                            
                            # Custom rule: Taptap Send charges ~0.99 for small EUR transfers
                            if from_country in ['DE', 'FR', 'IT', 'ES', 'NL', 'AT', 'BE'] and amount < 170:
                                fee = 0.99
                                
                            return {
                                "provider": "Taptap Send",
                                "rate": rate,
                                "fee": fee,
                                "received_amount": round((amount * rate) - fee, 2)
                            }
        except Exception as e:
            print(f"[{provider}] Error: {e}")
    return None


async def fetch_remit_choice(from_country: str, to_country: str, amount: float) -> Optional[Dict[str, Any]]:
    """Direct POST request scraping for RemitChoice (Bypassing proxy)"""
    url = "https://www.remitchoice.com/ajaxcalculator.php"
    
    country_mappings = {
        'AT': {'code': 'AUT', 'currency': 'EUR'},
        'US': {'code': 'USA', 'currency': 'USD'},
        'GB': {'code': 'GBR', 'currency': 'GBP'},
        'PK': {'code': 'PAK', 'currency': 'PKR'},
        'IN': {'code': 'IND', 'currency': 'INR'},
        'BD': {'code': 'BGD', 'currency': 'BDT'},
        'PH': {'code': 'PHL', 'currency': 'PHP'},
        'DE': {'code': 'DEU', 'currency': 'EUR'},
    }
    
    if from_country not in country_mappings or to_country not in country_mappings:
        return None
        
    payload = {
        'sending_country': country_mappings[from_country]['code'],
        'receiving_country': country_mappings[to_country]['code'],
        'sendingcurrencycode': country_mappings[from_country]['currency'],
        'receivingcurrencycode': country_mappings[to_country]['currency'],
        'payout_method': 'Bank',
        'payout_patner': 'Bank',
        'sending_amount': str(amount),
        'receiving_amount': '',
        'total_payable_amount': '',
        'action': 's_country'
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Referer': 'https://www.remitchoice.com/',
    }
    
    provider = "RemitChoice"
    async with httpx.AsyncClient() as client:
        try:
            response = await request_with_retry(client, provider, "POST", url, data=payload, headers=headers)
            if response is None:
                return None
            html = response.text
            
            rate_match = re.search(r'Exchange Rate 1 [A-Z]{3} = (\d+(?:\.\d+)?) [A-Z]{3}', html)
            fee_match = re.search(r'Transfer Fee: (\d+(?:\.\d+)?) [A-Z]{3}', html)
            
            if rate_match:
                exchange_rate = float(rate_match.group(1))
                transfer_fee = float(fee_match.group(1)) if fee_match else 0.0
                return {
                    "provider": "RemitChoice",
                    "rate": exchange_rate,
                    "fee": transfer_fee,
                    "received_amount": round((amount * exchange_rate) - transfer_fee, 2)
                }
        except Exception as e:
            print(f"[{provider}] Error: {e}")
            
    return None

# Simple in-memory cache to prevent LiteSpeed max children errors
RATE_CACHE = {}
CACHE_DURATION_SECONDS = 600  # Cache results for 10 minutes

async def fetch_remitly(from_country: str, to_country: str, amount: float) -> Optional[Dict[str, Any]]:
    """Scrapes Remitly embedded Next.js JSON without a browser."""
    from_lower = from_country.lower()
    to_lower = to_country.lower()
    
    # Remitly uses full country names in the URL for the destination, e.g., /gb/en/pakistan
    country_name_map = {
        'pk': 'pakistan',
        'in': 'india',
        'ph': 'philippines',
        'bd': 'bangladesh',
        'cn': 'china',
        'ng': 'nigeria',
        'eg': 'egypt',
        'ma': 'morocco',
        'ae': 'united-arab-emirates',
        'lk': 'sri-lanka'
    }
    
    to_name = country_name_map.get(to_lower)
    if not to_name:
        print(f"[Remitly] Destination {to_country} not mapped.")
        return None
        
    url = f"https://www.remitly.com/{from_lower}/en/{to_name}"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    provider = "Remitly"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await request_with_retry(client, provider, "GET", url, headers=headers)
            if response is None:
                return None
            html = response.text
            
            # Use smart regex to extract from HTML text rendered by server
            rate_match = re.search(r'1\s+([A-Z]{3})\s*=\s*(\d+(?:[.,]\d+)?)\s*([A-Z]{3})', html)
            fee_match = re.search(r'"feesAsLowAs":"([\d\.]+)"', html)
            
            if not rate_match:
                print("[Remitly] No rate match found in HTML!")
                return None
                
            exchange_rate = float(rate_match.group(2).replace(',', '.'))
            fee = float(fee_match.group(1)) if fee_match else 0.0
            
            return {
                "provider": "Remitly",
                "rate": exchange_rate,
                "fee": fee,
                "received_amount": round((amount * exchange_rate) - fee, 2)
            }
                
        except Exception as e:
            print(f"[{provider}] Network Error: {e}")
            
    return None

@app.get("/api/rates")
@app.get("/rates")
async def get_rates(
    from_country: str = Query(..., description="ISO 2 code representing the sending country (e.g., US, GB)"),
    to_country: str = Query(..., description="ISO 2 code representing the receiving country (e.g., IN, BD)"),
    amount: float = Query(100.0, description="Amount to be transferred")
):
    from_country = from_country.upper()
    to_country = to_country.upper()
    
    # Check if we have a valid cached result first!
    cache_key = f"{from_country}_{to_country}_{amount}"
    current_time = time.time()
    
    if cache_key in RATE_CACHE:
        cached_time, cached_results = RATE_CACHE[cache_key]
        # If the cache is younger than our duration (10 minutes)
        if current_time - cached_time < CACHE_DURATION_SECONDS:
            return cached_results
    
    # Run fetch operations concurrently so everything finishes faster
    tasks = [
        fetch_taptap_send(from_country, to_country, amount),
        fetch_remit_choice(from_country, to_country, amount),
        fetch_remitly(from_country, to_country, amount),
    ]
    
    results = await asyncio.gather(*tasks)
    
    # Filter out None results
    valid_rates = [res for res in results if res is not None]

    # Gracefully degrade to stale cache if every provider fails temporarily.
    if not valid_rates and cache_key in RATE_CACHE:
        cached_time, cached_results = RATE_CACHE[cache_key]
        if current_time - cached_time < 3600 and cached_results:
            return cached_results
    
    # Sort by who gives the recipient the most money (descending)
    valid_rates.sort(key=lambda x: x['received_amount'], reverse=True)
    
    # Save the successful result to our cache
    RATE_CACHE[cache_key] = (current_time, valid_rates)
    
    return valid_rates


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
