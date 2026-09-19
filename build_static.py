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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "data", "reports")
SIGNALS_FILE = os.path.join(BASE_DIR, "data", "signals.json")
DOCS_DIR = os.path.join(BASE_DIR, "docs")

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


def fetch_ohlc(coin, days=7):
    """Fetch OHLC data from CoinGecko for a coin."""
    coin_id = coingecko_id(coin)
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days={days}&interval=daily"
    headers = {}
    api_key = os.environ.get("COINGECKO_KEY")
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  CoinGecko error for {coin} ({coin_id}): {e.code}")
        return None
    except Exception as e:
        print(f"  CoinGecko error for {coin} ({coin_id}): {e}")
        return None
    
    prices = data.get("prices", [])
    if not prices:
        return None
    
    # Bucket into ~7 daily candles
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
            "c": opens[-1]
        })
    return ohlc


def fetch_all_chart_data(cards):
    """Fetch chart data for all coins in cards."""
    chart_data = {}
    for card in cards:
        coin = card["coin"]
        print(f"  Fetching chart data for {coin}...")
        ohlc = fetch_ohlc(coin, days=7)
        if ohlc:
            chart_data[coin] = ohlc
        else:
            print(f"    No data for {coin}")
        time.sleep(0.2)  # be nice to the API
    return chart_data


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
        return f"https://github.com/{repo}/actions/workflows/scan.yml"
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

    data = {
        "timestamp": ts,
        "cards": cards,
        "signals": len(signals),
        "workflow_url": workflow_url(),
        "chart_data": chart_data,
    }

    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(os.path.join(DOCS_DIR, "data.json"), "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote docs/data.json: {len(data['cards'])} cards, {data['signals']} signals, {len(chart_data)} chart datasets")


if __name__ == "__main__":
    main()
