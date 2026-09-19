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
    # Curated disambiguations: exotic/renamed tickers that the market-cap
    # listing or a plain symbol search would otherwise resolve to the wrong coin.
    'zano': 'zano',
    'zforge': 'zecforge-tech',
    'zama': 'zama',
    'drv': 'derive',
    'ake': 'akedo',
    'pons': 'pons',
    'prove': 'succinct',
    'saga': 'saga-2',
    'firo': 'zcoin',
    'trump': 'official-trump',
    'pengu': 'pudgy-penguins',
    'f': 'figure-heloc',
}


def fetch_json(url):
    global _COINGECKO_KEY_DISABLED
    api_key = None if _COINGECKO_KEY_DISABLED else os.environ.get("COINGECKO_KEY")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    for attempt in range(3):
        try:
            throttle_requests()
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
            wait = e.headers.get("Retry-After") if e.headers else None
            try:
                wait = float(wait) if wait else 1.5 * (attempt + 1)
            except (TypeError, ValueError):
                wait = 1.5 * (attempt + 1)
            time.sleep(min(wait, 30))
        except Exception as e:
            print(f"  CoinGecko error: {e}")
            time.sleep(0.5 * (attempt + 1))
    return None


def fetch_ohlc_candles(coin_id, days):
    """True OHLC candles from CoinGecko.

    Granularity is decided by CoinGecko: 30-minute candles for days=1 and
    4-hour candles for days=7/30.
    """
    url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
           f"?vs_currency=usd&days={days}")
    rows = fetch_json(url)
    if not isinstance(rows, list):
        return None
    candles = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            timestamp = int(row[0])
            open_, high, low, close = (float(row[1]), float(row[2]),
                                       float(row[3]), float(row[4]))
        except (TypeError, ValueError):
            continue
        if min(open_, high, low, close) <= 0:
            continue
        candles.append({
            "x": timestamp,
            "o": open_,
            "h": high,
            "l": low,
            "c": close,
        })
    return candles or None


def fetch_price_points(coin_id, days):
    """Fallback history as (timestamp_ms, price) points when OHLC is missing."""
    url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
           f"?vs_currency=usd&days={days}")
    data = fetch_json(url)
    if not isinstance(data, dict):
        return None
    points = []
    for entry in data.get("prices") or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        try:
            points.append((int(entry[0]), float(entry[1])))
        except (TypeError, ValueError):
            continue
    return points or None


TIMEFRAMES = ("1d", "7d", "30d")
CANDLE_TARGETS = {"1d": 48, "7d": 42, "30d": 90}
# CoinGecko's public API is rate limited (~tens of calls/minute), so every
# request is spaced out globally instead of hammering the endpoint.
MIN_REQUEST_INTERVAL = float(os.environ.get("CG_MIN_INTERVAL", "2.2"))
_last_request_at = [0.0]


def throttle_requests():
    elapsed = time.time() - _last_request_at[0]
    wait = MIN_REQUEST_INTERVAL - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_request_at[0] = time.time()


def clean_points(points):
    """Normalise loose (timestamp, price) data, dropping anything unusable."""
    cleaned = []
    for point in points or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            timestamp = int(point[0])
            price = float(point[1])
        except (TypeError, ValueError):
            continue
        if price > 0:
            cleaned.append((timestamp, price))
    return cleaned


def points_to_candles(points, buckets):
    """Aggregate loose price points into `buckets` OHLC candles, oldest first."""
    cleaned = clean_points(points)
    if len(cleaned) < 2 or buckets < 1:
        return None
    buckets = min(buckets, len(cleaned))
    candles = []
    for i in range(buckets):
        start = int(i * len(cleaned) / buckets)
        end = int((i + 1) * len(cleaned) / buckets)
        if end <= start:
            end = start + 1
        window = cleaned[start:end]
        if not window:
            continue
        values = [price for _, price in window]
        candles.append({
            "x": window[0][0],
            "o": values[0],
            "h": max(values),
            "l": min(values),
            "c": values[-1],
        })
    return candles or None


def downsample_candles(candles, target):
    """Merge adjacent candles into at most `target` candles, keeping OHLC meaning."""
    if not candles:
        return None
    if target < 1 or len(candles) <= target:
        return list(candles)
    merged = []
    total = len(candles)
    for i in range(target):
        start = int(i * total / target)
        end = int((i + 1) * total / target)
        if end <= start:
            end = start + 1
        window = candles[start:end]
        if not window:
            continue
        merged.append({
            "x": window[0]["x"],
            "o": window[0]["o"],
            "h": max(candle["h"] for candle in window),
            "l": min(candle["l"] for candle in window),
            "c": window[-1]["c"],
        })
    return merged or None


def fill_missing_timeframes(series):
    """Guarantee every timeframe has candles so the UI always has something."""
    series = {tf: candles for tf, candles in series.items() if candles}
    if "1d" not in series and series.get("7d"):
        weekly = series["7d"]
        series["1d"] = weekly[-6:] if len(weekly) > 6 else list(weekly)
    if "7d" not in series and series.get("30d"):
        monthly = series["30d"]
        series["7d"] = monthly[-CANDLE_TARGETS["7d"]:] if len(monthly) > CANDLE_TARGETS["7d"] else list(monthly)
    if "30d" not in series and series.get("7d"):
        series["30d"] = list(series["7d"])
    if "7d" not in series and series.get("1d"):
        series["7d"] = list(series["1d"])
    return series or None


def build_chart_series(coin_id):
    """Build 1d / 7d / 30d candle sets for one CoinGecko id (None when unknown)."""
    series = {}

    intraday = fetch_ohlc_candles(coin_id, 1)
    if intraday:
        series["1d"] = downsample_candles(intraday, CANDLE_TARGETS["1d"])

    monthly = fetch_ohlc_candles(coin_id, 30)
    if monthly:
        weekly = (monthly[-CANDLE_TARGETS["7d"]:]
                  if len(monthly) > CANDLE_TARGETS["7d"] else list(monthly))
        series["7d"] = weekly
        series["30d"] = downsample_candles(monthly, CANDLE_TARGETS["30d"])

    # Some coins have no /ohlc history at all - rebuild candles from prices.
    if not series.get("1d") or not series.get("7d") or not series.get("30d"):
        month_points = fetch_price_points(coin_id, 30)
        if month_points:
            if not series.get("30d"):
                series["30d"] = points_to_candles(month_points, CANDLE_TARGETS["30d"])
            if not series.get("7d"):
                series["7d"] = points_to_candles(month_points[-168:], CANDLE_TARGETS["7d"])
        if not series.get("1d"):
            day_points = fetch_price_points(coin_id, 1)
            if day_points:
                series["1d"] = points_to_candles(day_points, CANDLE_TARGETS["1d"])

    return fill_missing_timeframes(series)


def series_from_sparkline(prices):
    """Last-resort candles from a 7-day hourly sparkline (24 hourly points/day)."""
    points = clean_points(prices)
    if len(points) < 2:
        return None
    series = {
        "1d": points_to_candles(points[-24:], 24),
        "7d": points_to_candles(points, CANDLE_TARGETS["7d"]),
    }
    return fill_missing_timeframes(series)


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


def search_coin_candidates(symbol):
    """CoinGecko ids that actually carry this exact ticker, best ranked first."""
    encoded = urllib.parse.quote(symbol)
    data = fetch_json(f"https://api.coingecko.com/api/v3/search?query={encoded}")
    if not isinstance(data, dict):
        return []
    target = symbol.upper()
    ranked = []
    for coin in data.get("coins") or []:
        coin_id = coin.get("id")
        if not coin_id or str(coin.get("symbol", "")).upper() != target:
            continue
        rank = coin.get("market_cap_rank")
        ranked.append((rank if isinstance(rank, int) else 10 ** 6, coin_id))
    ranked.sort(key=lambda item: item[0])
    return [coin_id for _, coin_id in ranked]


def signal_coin_ids(signals):
    """Exact CoinGecko ids recorded by the agents (coin, symbol) -> coin_id."""
    mapping = {}
    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        coin_id = (signal.get("details") or {}).get("coin_id")
        if not coin_id:
            continue
        for key in (signal.get("coin"), signal.get("symbol")):
            if key:
                mapping.setdefault(str(key).upper(), coin_id)
    return mapping


def resolve_coin_series(coin, markets, signal_ids=None):
    """Resolve one card symbol to (coin_id, candle series) - or (None, None).

    Order of attempts: the id the agents already recorded for this coin, the
    curated id map, the symbol match from the market listing, then exact-ticker
    search results. Every candidate must return real candles before it is
    accepted, which is what keeps wrong lookalike tickers out.
    """
    tried = set()
    ordered = []
    if signal_ids and signal_ids.get(coin.upper()):
        ordered.append(signal_ids[coin.upper()])
    preferred = COINGECKO_ID_MAP.get(coin.lower())
    if preferred:
        ordered.append(preferred)
    market = markets.get(coin.upper())
    if market and market.get("id"):
        ordered.append(market["id"])

    for coin_id in ordered:
        if coin_id in tried:
            continue
        tried.add(coin_id)
        series = build_chart_series(coin_id)
        if series:
            return coin_id, series

    for coin_id in search_coin_candidates(coin)[:3]:
        if coin_id in tried:
            continue
        tried.add(coin_id)
        series = build_chart_series(coin_id)
        if series:
            return coin_id, series

    return None, None


def fetch_all_chart_data(cards, signals=None):
    """Build premium candle data for every coin card (returns data + stats)."""
    chart_data = {}
    market_stats = {}
    symbols = [card["coin"] for card in cards]
    signal_ids = signal_coin_ids(signals)
    print("  Resolving CoinGecko market data...")
    markets = fetch_market_sparklines(symbols)

    seen = set()
    for card in cards:
        coin = card["coin"]
        if coin in seen:
            continue
        seen.add(coin)

        coin_id, series = resolve_coin_series(coin, markets, signal_ids)
        if not series:
            sparkline = (markets.get(coin.upper()) or {}).get("sparkline_in_7d", {}).get("price")
            if sparkline:
                series = series_from_sparkline(sparkline)
                if series:
                    print(f"  {coin}: fell back to the 7d sparkline")

        if series:
            chart_data[coin] = series
            market_stats[coin] = derive_market_stats(coin_id, series)
            print(f"  Chart data ready for {coin} "
                  f"({', '.join(f'{len(series[tf])}x{tf}' for tf in TIMEFRAMES if series.get(tf))})")
        else:
            print(f"    No data for {coin}")

    missing = [card["coin"] for card in cards if card["coin"] not in chart_data]
    if missing:
        print(f"  No chart data for: {', '.join(missing)}")
    return chart_data, market_stats


def pct_change(start, end):
    if not start or end is None:
        return None
    return (end - start) / start * 100


def derive_market_stats(coin_id, series):
    """Derive the modal's market stats from the embedded candles."""
    stats = {"coin_id": coin_id, "source": "coingecko"}
    daily = series.get("1d") or []
    weekly = series.get("7d") or []
    monthly = series.get("30d") or []
    reference = daily or weekly or monthly

    if reference:
        stats["price"] = reference[-1].get("c")
        stats["high24h"] = max(candle["h"] for candle in reference)
        stats["low24h"] = min(candle["l"] for candle in reference)

    if len(daily) > 1:
        stats["change24h"] = pct_change(daily[0].get("o"), daily[-1].get("c"))
    elif len(reference) > 1:
        stats["change24h"] = pct_change(reference[-2].get("c"), reference[-1].get("c"))
    if weekly:
        stats["change7d"] = pct_change(weekly[0].get("o"), weekly[-1].get("c"))
    if monthly:
        stats["change30d"] = pct_change(monthly[0].get("o"), monthly[-1].get("c"))
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
    chart_data, market_stats = fetch_all_chart_data(cards, signals)
    print(f"  Got chart data for {len(chart_data)}/{len(cards)} coins")

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
