import os
import re
import asyncio
import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict, Any

app = FastAPI(title="Remittance Rates API")

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
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=4.0)
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
            print(f"[Taptap Send] Error: {e}")
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
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, data=payload, headers=headers, timeout=4.0)
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
            print(f"[RemitChoice] Error: {e}")
            
    return None

import json
import time

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
    
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers, timeout=4.0)
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
            print(f"[Remitly] Network Error: {e}")
            
    return None

import urllib.parse
async def fetch_ace_money(from_country: str, to_country: str, amount: float) -> Optional[Dict[str, Any]]:
    """Scrapes ACE Money Transfer using their make-request proxy endpoint."""
    
    numeric_codes = {
        'AT': '040', 'US': '840', 'GB': '826', 'PK': '586', 
        'IN': '356', 'BD': '050', 'PH': '608', 'DE': '276', 
        'FR': '250', 'ES': '724', 'IT': '380', 'NL': '528', 
        'CN': '156', 'AE': '784', 'BE': '056'
    }
    currency_map = {
        'AT': 'EUR', 'DE': 'EUR', 'FR': 'EUR', 'IT': 'EUR', 
        'ES': 'EUR', 'NL': 'EUR', 'BE': 'EUR', 'US': 'USD', 
        'GB': 'GBP', 'AE': 'AED'
    }
    
    src_numeric = numeric_codes.get(from_country)
    dest_numeric = numeric_codes.get(to_country)
    user_currency = currency_map.get(from_country, 'EUR')
    
    if not src_numeric or not dest_numeric:
        print(f"[ACE Money] Missing numeric mapping for {from_country} -> {to_country}")
        return None
        
    url = "https://acemoneytransfer.com/make-request"
    
    headers = {
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Origin': 'https://acemoneytransfer.com',
        'Referer': 'https://acemoneytransfer.com/',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36',
        'X-Requested-With': 'XMLHttpRequest'
    }
    
    payload = {
        'uri': 'rate/calculator',
        'type': 'POST',
        'data[src_amount]': str(amount),
        'data[dest_amount]': '',
        'data[g-recaptcha-response]': '',
        'data[calculate]': '1',
        'data[user_currency]': user_currency,
        'data[calculation_mode]': 'S',
        'data[dest_iso_numeric_code]': dest_numeric,
        'data[src_iso_numeric_code]': src_numeric,
        'auth': 'true'
    }
    
    # ACE often requires specific payer IDs for specific corridors. 
    # If we know them, we can append them. '40' represents an Indian receiving method maybe?
    if to_country == 'IN':
        payload['data[sepecific_payer_id]'] = '40'
    elif to_country == 'PK':
        # Let's try appending nothing, or a known PK id if we had one.
        pass
        
    encoded_data = urllib.parse.urlencode(payload)
    
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            # 1. First get cookies and XSRF
            home_res = await client.get("https://acemoneytransfer.com/", headers={'User-Agent': headers['User-Agent']}, timeout=4.0)
            
            # The Laravel CSRF token is in the cookies as XSRF-TOKEN
            xsrf = client.cookies.get('XSRF-TOKEN')
            if xsrf:
                headers['X-XSRF-TOKEN'] = urllib.parse.unquote(xsrf)
                
            # 2. Make the POST request
            res = await client.post(url, headers=headers, content=encoded_data, timeout=4.0)
            
            data = res.json()
            if data and isinstance(data, dict):
                # The response here usually drops a JSON with embedded calculation results or HTML
                # Let's inspect standard ACE response structures
                
                # Typical ace success payload has 'data' child that contains arrays or another dict.
                if 'data' in data:
                    inner_data = data['data']
                    if isinstance(inner_data, dict):
                        # They pass back rate and fee
                        rate = float(inner_data.get('exchange_rate', 0.0))
                        fee = float(inner_data.get('fee', 0.0))
                        
                        # Sometimes they return 'source_amount' and 'destination_amount'
                        if rate == 0 and 'destination_amount' in inner_data:
                            dest_amt = float(inner_data['destination_amount'])
                            rate = round(dest_amt / amount, 4)
                            
                        if rate > 0:
                            return {
                                "provider": "ACE Money Transfer",
                                "rate": rate,
                                "fee": fee,
                                "received_amount": round((amount * rate) - fee, 2)
                            }
                
                # Fallback: Print what their structure actually is if standard fails
                print(f"[ACE Money] Debug structure: {str(data)[:250]}")
                
        except Exception as e:
            print(f"[ACE Money] Network Error: {e}")
            
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
        fetch_ace_money(from_country, to_country, amount)
    ]
    
    results = await asyncio.gather(*tasks)
    
    # Filter out None results
    valid_rates = [res for res in results if res is not None]
    
    # Sort by who gives the recipient the most money (descending)
    valid_rates.sort(key=lambda x: x['received_amount'], reverse=True)
    
    # Save the successful result to our cache
    RATE_CACHE[cache_key] = (current_time, valid_rates)
    
    return valid_rates