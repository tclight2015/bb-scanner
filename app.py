import asyncio
import aiohttp
import json
import time
import math
from flask import Flask, jsonify, render_template
from datetime import datetime

app = Flask(__name__)

BINANCE_BASE = "https://fapi.binance.com"

# Cache
cache = {
    "data": [],
    "last_updated": None,
    "is_scanning": False,
    "volume_data": {}  # symbol -> 24h quote volume
}

async def fetch_json(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()
    except:
        return None

async def get_all_symbols(session):
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/exchangeInfo")
    if not data:
        return []
    symbols = [
        s["symbol"] for s in data["symbols"]
        if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
    ]
    return symbols

async def get_funding_intervals(session):
    exinfo = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/exchangeInfo")
    hourly_symbols = set()
    if exinfo:
        for s in exinfo.get("symbols", []):
            interval = s.get("fundingIntervalHours", 8)
            if interval == 1:
                hourly_symbols.add(s["symbol"])
    return hourly_symbols

async def get_24h_volumes(session):
    """Get 24h quote volume for all symbols"""
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/ticker/24hr")
    volumes = {}
    if data:
        for item in data:
            sym = item.get("symbol", "")
            vol = float(item.get("quoteVolume", 0))
            volumes[sym] = vol
    return volumes

async def get_klines(session, symbol):
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/klines", {
        "symbol": symbol,
        "interval": "15m",
        "limit": 25
    })
    return data

def calc_bollinger(klines, period=20, std_mult=2.0):
    if not klines or len(klines) < period:
        return None
    closes = [float(k[4]) for k in klines]
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = mean + std_mult * std
    lower = mean - std_mult * std
    current_price = closes[-1]
    return {
        "price": current_price,
        "upper": upper,
        "middle": mean,
        "lower": lower,
        "std": std
    }

async def scan_symbol(session, symbol, volume_usdt):
    klines = await get_klines(session, symbol)
    if not klines:
        return None
    bb = calc_bollinger(klines)
    if not bb:
        return None
    price = bb["price"]
    upper = bb["upper"]
    middle = bb["middle"]
    if price >= upper:
        return None
    band_width_pct = (upper - middle) / middle * 100
    if band_width_pct < 1.0:
        return None
    dist_to_upper_pct = (upper - price) / upper * 100
    return {
        "symbol": symbol.replace("USDT", ""),
        "full_symbol": symbol,
        "price": price,
        "upper": upper,
        "middle": middle,
        "lower": bb["lower"],
        "dist_to_upper_pct": dist_to_upper_pct,
        "band_width_pct": band_width_pct,
        "volume_usdt": volume_usdt
    }

async def run_scan():
    cache["is_scanning"] = True
    results = []

    async with aiohttp.ClientSession() as session:
        symbols = await get_all_symbols(session)
        if not symbols:
            cache["is_scanning"] = False
            return

        hourly_symbols = await get_funding_intervals(session)
        volumes = await get_24h_volumes(session)
        cache["volume_data"] = volumes

        symbols = [s for s in symbols if s not in hourly_symbols]

        batch_size = 20
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i+batch_size]
            tasks = [scan_symbol(session, sym, volumes.get(sym, 0)) for sym in batch]
            batch_results = await asyncio.gather(*tasks)
            for r in batch_results:
                if r:
                    results.append(r)
            await asyncio.sleep(0.15)

    results.sort(key=lambda x: x["dist_to_upper_pct"])
    cache["data"] = results
    cache["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cache["is_scanning"] = False

def run_scan_sync():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_scan())
    loop.close()

import threading

def background_scanner():
    while True:
        if not cache["is_scanning"]:
            run_scan_sync()
        time.sleep(60)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/data")
def api_data():
    return jsonify({
        "data": cache["data"],
        "last_updated": cache["last_updated"],
        "is_scanning": cache["is_scanning"],
        "count": len(cache["data"])
    })

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if not cache["is_scanning"]:
        t = threading.Thread(target=run_scan_sync)
        t.daemon = True
        t.start()
    return jsonify({"status": "started"})

if __name__ == "__main__":
    scanner_thread = threading.Thread(target=background_scanner)
    scanner_thread.daemon = True
    scanner_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
