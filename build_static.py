#!/usr/bin/env python3
"""
Build the static dashboard data for GitHub Pages.

Runs the agent pipeline (unless --no-run), then converts the latest report
and signals into a single docs/data.json that docs/index.html consumes.

Run locally:   python3 build_static.py            (runs a fresh scan)
               python3 build_static.py --no-run   (just re-render from last scan)
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime
import urllib.request
import urllib.error
import urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "data", "reports")
SIGNALS_FILE = os.path.join(BASE_DIR, "data", "signals.json")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
_COINGECKO_KEY_DISABLED = False

COINGECKO_ID_MAP = {
    'btc': 'bitcoin',
    'sol': 'solana',
    'near': 'near',
    'storj': 'storj',
    'zec': 'zcash',
    'lsk': 'lisk',
    'eth': 'ethereum',
    'arb': 'arbitrum',
    'op': 'optimism',
    'matic': 'matic-network',
    'avax': 'avalanche-2',
    'dot': 'polkadot',
    'link': 'chainlink',
    'uni': 'uniswap',
    'aave': 'aave',
    'snx': 'havven',
    'crv': 'curve-dao-token',
    'sushi': 'sushi',
    'yfi': 'yearn-finance',
    'comp': 'compound-governance-token',
    'mkr': 'maker',
    'ldo': 'lido-dao',
    'rpl': 'rocket-pool',
}


def coingecko_id(symbol):
    return COINGECKO_ID_MAP.get(symbol.lower(), symbol.lower())


def fetch_json(url):
    global _COINGECKO_KEY_DISABLED
    api_key = None if _COINGECKO_KEY_DISABLED else os.environ.get("COINGECKO_KEY")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401 and api_key:
                _COINGECKO_KEY_DISABLED = True
                api_key = None
                headers = {"Accept": "application/json"}
                print("  CoinGecko key rejected; retrying without a key")
                continue
            if e.code != 429:
                print(f"  CoinGecko error: {e.code}")
                return None
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            print(f"  CoinGecko error: {e}")
            time.sleep(0.5 * (attempt + 1))
    return None


def fetch_ohlc_for_id(coin, coin_id, days=7):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days={days}&interval=daily"
    data = fetch_json(url)
    if not isinstance(data, dict):
        return None
    prices = data.get("prices", [])
    if not prices:
        return None
    buckets = 7
    bucket_size = max(1, len(prices) // buckets)
    ohlc = []
    for i in range(buckets):
        start = i * bucket_size
        end = min((i + 1) * bucket_size, len(prices))
        if start >= end:
            break
        slice_ = prices[start:end]
        opens = [p[1] for p in slice_]
        timestamps = [p[0] for p in slice_]
        ohlc.append({
            "x": timestamps[0],
            "o": opens[0],
            "h": max(opens),
            "l": min(opens),
            "c": opens[-1],
        })
    return ohlc


def fetch_ohlc(coin, days=7):
    return fetch_ohlc_for_id(coin, coingecko_id(coin), days)


def sparkline_to_ohlc(prices):
    points = []
    for point in prices:
        if not isinstance(point, list) or len(point) < 2:
            continue
        try:
            points.append((int(point[0]), float(point[1])))
        except (TypeError, ValueError):
            continue
    if len(points) < 2:
        return None
    buckets = 7
    ohlc = []
    for i in range(buckets):
        start = int(i * len(points) / buckets)
        end = int((i + 1) * len(points) / buckets)
        if end <= start:
            end = start + 1
        slice_ = points[start:end]
        values = [point[1] for point in slice_]
        ohlc.append({
            "x": slice_[0][0],
            "o": values[0],
            "h": max(values),
            "l": min(values),
            "c": values[-1],
        })
    return ohlc


def fetch_market_sparklines(symbols):
    wanted = {symbol.upper() for symbol in symbols}
    resolved = {}
    for page in range(1, 11):
        url = (
            "https://api.coingecko.com/api/v3/coins/markets"
            f"?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=true"
        )
        data = fetch_json(url)
        if not isinstance(data, list):
            break
        for coin in data:
            symbol = str(coin.get("symbol", "")).upper()
            if symbol not in wanted:
                continue
            preferred_id = COINGECKO_ID_MAP.get(symbol.lower())
            coin_id = coin.get("id")
            if symbol not in resolved or (preferred_id and coin_id == preferred_id):
                resolved[symbol] = coin
        if wanted.issubset(resolved):
            break
        time.sleep(0.2)
    return resolved


def search_coin_id(symbol):
    known_id = COINGECKO_ID_MAP.get(symbol.lower())
    if known_id:
        return known_id
    encoded = urllib.parse.quote(symbol)
    data = fetch_json(f"https://api.coingecko.com/api/v3/search?query={encoded}")
    if not isinstance(data, dict):
        return None
    target = symbol.upper()
    for coin in data.get("coins", []):
        if str(coin.get("symbol", "")).upper() == target:
            return coin.get("id")
    return None


def fetch_all_chart_data(cards):
    chart_data = {}
    symbols = [card["coin"] for card in cards]
    print("  Resolving CoinGecko market data...")
    markets = fetch_market_sparklines(symbols)
    for card in cards:
        coin = card["coin"]
        symbol = coin.upper()
        market = markets.get(symbol)
        coin_id = market.get("id") if market else search_coin_id(coin)
        ohlc = None
        if market:
            ohlc = sparkline_to_ohlc(market.get("sparkline_in_7d", {}).get("price", []))
        if not ohlc and coin_id:
            ohlc = fetch_ohlc_for_id(coin, coin_id, days=7)
        if ohlc:
            chart_data[coin] = ohlc
            print(f"  Chart data ready for {coin}")
        else:
            print(f"    No data for {coin}")
    return chart_data


def derive_market_stats(chart_data):
    """Derive static market stats from the embedded chart data."""
    stats = {}
    for coin, candles in chart_data.items():
        if not candles:
            continue
        last = candles[-1]
        prev = candles[-2] if len(candles) > 1 else candles[0]
        change24h = None
        if prev and prev.get("c"):
            change24h = ((last.get("c") or 0) - prev["c"]) / prev["c"] * 100
        change7d = None
        if candles[0].get("c"):
            change7d = ((last.get("c") or 0) - candles[0]["c"]) / candles[0]["c"] * 100
        stats[coin] = {
            "high24h": last.get("h"),
            "low24h": last.get("l"),
            "change24h": change24h,
            "change7d": change7d,
        }
    return stats


def parse_report_cards(md):
    """Convert the markdown report into structured card objects for the frontend."""
    cards = []
    current = None
    for line in md.split("\n"):
        line = line.strip()
        if line.startswith("## "):
            if current:
                cards.append(current)
            title = line[3:].strip()
            action = "WATCH"
            if "BUY" in title:
                action = "BUY"
            elif "AVOID" in title:
                action = "AVOID"
            coin = title.split("—")[0].strip() if "—" in title else title
            coin = coin.lstrip("🟢🟡🔴 ").strip()
            current = {"coin": coin, "action": action, "details": []}
        elif current and (line.startswith("- **") or line.startswith("- ")):
            current["details"].append(line[2:].strip())
    if current:
        cards.append(current)
    return cards


def get_latest_report():
    files = sorted(glob.glob(os.path.join(REPORTS_DIR, "report_*.md")))
    if not files:
        return None, None
    with open(files[-1], "r") as f:
        return f.read(), files[-1]


def run_pipeline():
    """Run the full agent pipeline. Non-fatal on failure (returns last report)."""
    print("Running agent pipeline...")
    try:
        subprocess.run(
            [sys.executable, os.path.join(BASE_DIR, "run_pipeline.py")],
            capture_output=True, text=True, timeout=180, cwd=BASE_DIR,
        )
    except Exception as e:
        print(f"  pipeline issue (continuing with last report): {e}")


def workflow_url():
    """GitHub Actions sets GITHUB_REPOSITORY=owner/repo. Local runs fall back."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        return f"https://github.com/{repo}/actions/workflows/pipeline.yml"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-run", action="store_true",
                    help="don't run the pipeline, just re-render last report")
    args = ap.parse_args()

    if not args.no_run:
        run_pipeline()

    md, path = get_latest_report()
    if md is None:
        print("No report found. Run a scan first.")
        sys.exit(1)

    # Timestamp from the report filename if possible
    ts = None
    fname = os.path.basename(path).replace("report_", "").replace(".md", "")
    try:
        ts = datetime.strptime(fname, "%Y%m%d_%H%M%S").isoformat()
    except Exception:
        ts = datetime.now().isoformat()

    signals = []
    if os.path.exists(SIGNALS_FILE):
        with open(SIGNALS_FILE, "r") as f:
            signals = json.load(f)

    cards = parse_report_cards(md)
    
    # Fetch chart data
    print("\nFetching chart data from CoinGecko...")
    chart_data = fetch_all_chart_data(cards)
    print(f"  Got chart data for {len(chart_data)} coins")
    market_stats = derive_market_stats(chart_data)

    data = {
        "timestamp": ts,
        "cards": cards,
        "signals": len(signals),
        "workflow_url": workflow_url(),
        "chart_data": chart_data,
        "market_stats": market_stats,
    }

    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(os.path.join(DOCS_DIR, "data.json"), "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote docs/data.json: {len(data['cards'])} cards, {data['signals']} signals, {len(chart_data)} chart datasets")


if __name__ == "__main__":
    main()
