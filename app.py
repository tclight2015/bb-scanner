import asyncio
import aiohttp
import time
import math
from flask import Flask, jsonify, render_template
from datetime import datetime

app = Flask(__name__)

BINANCE_BASE = "https://fapi.binance.com"

cache = {
    "data": [],
    "last_updated": None,
    "is_scanning": False,
    "volume_data": {}
}

async def fetch_json(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                return await r.json()
            return None
    except Exception as e:
        print(f"fetch_json error {url}: {e}")
        return None

async def get_all_symbols(session):
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/exchangeInfo")
    if not data or not isinstance(data, dict):
        print("exchangeInfo returned invalid data")
        return []
    symbols_raw = data.get("symbols", [])
    if not symbols_raw:
        print("exchangeInfo symbols list is empty")
        return []
    symbols = [
        s["symbol"] for s in symbols_raw
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
    ]
    print(f"Found {len(symbols)} symbols")
    return symbols

async def get_funding_intervals(session):
    exinfo = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/exchangeInfo")
    hourly_symbols = set()
    if exinfo and isinstance(exinfo, dict):
        for s in exinfo.get("symbols", []):
            interval = s.get("fundingIntervalHours", 8)
            if interval == 1:
                hourly_symbols.add(s["symbol"])
    print(f"Hourly funding symbols to exclude: {len(hourly_symbols)}")
    return hourly_symbols

async def get_24h_volumes(session):
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/ticker/24hr")
    volumes = {}
    if data and isinstance(data, list):
        for item in data:
            sym = item.get("symbol", "")
            vol = float(item.get("quoteVolume", 0))
            volumes[sym] = vol
    print(f"Got volume data for {len(volumes)} symbols")
    return volumes

async def get_klines(session, symbol, interval="15m", limit=25):
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    })
    return data

def calc_bollinger(klines, period=20, std_mult=2.0):
    if not klines or not isinstance(klines, list) or len(klines) < period:
        return None
    try:
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
        }
    except Exception as e:
        print(f"calc_bollinger error: {e}")
        return None

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
    print(f"Scan started at {datetime.now()}")

    try:
        async with aiohttp.ClientSession() as session:
            symbols = await get_all_symbols(session)
            if not symbols:
                print("No symbols found, aborting scan")
                cache["is_scanning"] = False
                return

            hourly_symbols = await get_funding_intervals(session)
            volumes = await get_24h_volumes(session)
            cache["volume_data"] = volumes

            symbols = [s for s in symbols if s not in hourly_symbols]
            print(f"Scanning {len(symbols)} symbols after exclusions")

            batch_size = 20
            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i+batch_size]
                tasks = [scan_symbol(session, sym, volumes.get(sym, 0)) for sym in batch]
                batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in batch_results:
                    if r and not isinstance(r, Exception):
                        results.append(r)
                await asyncio.sleep(0.2)

        results.sort(key=lambda x: x["dist_to_upper_pct"])
        cache["data"] = results
        cache["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"Scan complete: {len(results)} results")

    except Exception as e:
        print(f"run_scan error: {e}")
    finally:
        cache["is_scanning"] = False

def run_scan_sync():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_scan())
    finally:
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
