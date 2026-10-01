"""
BaseAgent — shared foundation for all crypto agents.
Handles signal bus read/write, memory persistence, and learning.
"""
import functools
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.json")
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")


def ensure_dirs():
    os.makedirs(MEMORY_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)


# Japan Standard Time (UTC+9, no DST) — used for every user-facing scan time.
JST = timezone(timedelta(hours=9), "JST")


def now_jst():
    return datetime.now(JST)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def api_get(url: str, params: Optional[dict] = None, tries: int = 3):
    """GET with small backoff on rate-limits/errors. Returns parsed JSON or None."""
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            return r.json()
        except (requests.exceptions.RequestException, ValueError):
            time.sleep(0.5 * (attempt + 1))
    return None


_master_list = None  # manual cache: only a successful fetch is stored
_ranked_map = None


def coin_master_list() -> list:
    """Full CoinGecko coin list — cached, but only successes (a failed
    first fetch must not poison the cache for the rest of the run)."""
    global _master_list
    if _master_list is None:
        data = api_get("https://api.coingecko.com/api/v3/coins/list")
        if isinstance(data, list) and data:
            _master_list = data
        else:
            return []
    return _master_list


def ranked_coin_ids() -> dict:
    """Symbol → id for the top-~200 ranked coins (authoritative mapping)."""
    global _ranked_map
    if _ranked_map is None:
        data = api_get("https://api.coingecko.com/api/v3/coins/markets",
                       params={"vs_currency": "usd", "order": "market_cap_desc",
                               "per_page": 200, "page": 1, "sparkline": "false"})
        ranked = {}
        if isinstance(data, list):
            for c in data:
                ranked.setdefault(c["symbol"].upper(), c["id"])
        _ranked_map = ranked
    return _ranked_map


# Curated fallbacks for tickers where alphabetic first-match is wrong
# (CoinGecko contains many duplicate symbols, e.g. BTC is also 'batcat').
KNOWN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin", "SOL": "solana",
    "TRX": "tron", "DOT": "polkadot", "LINK": "chainlink",
    "MATIC": "matic-network", "LTC": "litecoin", "AVAX": "avalanche-2",
    "UNI": "uniswap", "XLM": "stellar", "ATOM": "cosmos",
    "FIL": "filecoin", "NEAR": "near", "ARB": "arbitrum", "OP": "optimism",
    "BCH": "bitcoin-cash", "SUI": "sui", "TON": "the-open-network",
    "SHIB": "shiba-inu", "PEPE": "pepe", "PENGU": "pudgy-penguins",
    "ENA": "ethena", "HNT": "helium", "LIT": "lighter",
    "USDT": "tether", "USDC": "usd-coin", "DAI": "dai",
    "WIF": "dogwifcoin", "JUP": "jupiter-exchange-solana",
}


def coin_id_for(symbol: str) -> Optional[str]:
    """Resolve a ticker to a CoinGecko id, preferring curated + ranked coins."""
    s = symbol.upper()
    if s in KNOWN_IDS:
        return KNOWN_IDS[s]
    if s in ranked_coin_ids():
        return ranked_coin_ids()[s]
    for c in coin_master_list():
        if c.get("symbol", "").upper() == s:
            return c["id"]
    return None


class BaseAgent:
    name = "base"
    emoji = "?"
    publish_results = True  # False for terminal consumers (advisor)

    def __init__(self):
        ensure_dirs()
        self.memory_path = os.path.join(MEMORY_DIR, f"{self.name}_memory.json")
        self.memory = self._load_memory()

    # ── Signal Bus ──────────────────────────────────────────────────────
    def read_signals(self) -> list:  # list[dict]
        if not os.path.exists(SIGNALS_FILE):
            return []
        with open(SIGNALS_FILE, "r") as f:
            return json.load(f)

    def write_signals(self, signals: list):
        """Merge new signals into the bus (dedup by coin+source)."""
        existing = self.read_signals()
        seen = {(s["coin"], s.get("source", "")) for s in existing}
        for sig in signals:
            sig.setdefault("timestamp", now_iso())
            sig.setdefault("agent", self.name)
            key = (sig["coin"], sig.get("source", ""))
            if key not in seen:
                existing.append(sig)
                seen.add(key)
        with open(SIGNALS_FILE, "w") as f:
            json.dump(existing, f, indent=2)

    def add_signal(self, coin: str, signal_type: str, details: dict,
                   confidence: float = 0.5, source: str = ""):
        self.write_signals([{
            "coin": coin.upper(),
            "signal": signal_type,
            "confidence": round(confidence, 2),
            "source": source,
            **details,
        }])

    # ── Memory & Learning ───────────────────────────────────────────────
    def _load_memory(self) -> dict:
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r") as f:
                return json.load(f)
        return {
            "predictions": [],
            "weights": self.default_weights(),
            "runs": 0,
            "accuracy_history": [],
        }

    def default_weights(self) -> dict:
        """Override in subclasses to define signal weights."""
        return {}

    def save_memory(self):
        with open(self.memory_path, "w") as f:
            json.dump(self.memory, f, indent=2)

    def record_prediction(self, coin: str, signal: str, confidence: float,
                          price_at_prediction: float):
        self.memory["predictions"].append({
            "coin": coin,
            "signal": signal,
            "confidence": confidence,
            "price": price_at_prediction,
            "timestamp": now_iso(),
            "outcome_24h": None,
            "outcome_7d": None,
        })
        self.memory["runs"] += 1
        self.save_memory()

    def learn_from_outcomes(self):
        """Check past predictions against current prices and update weights."""
        try:
            import requests
        except ImportError:
            return

        updated = False
        for pred in self.memory["predictions"]:
            if pred["outcome_24h"] is not None:
                continue
            if not pred.get("coin"):
                continue
            try:
                r = requests.get(
                    f"https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": pred["coin"].lower(), "vs_currencies": "usd"},
                    timeout=10,
                )
                data = r.json()
                coin_id = pred["coin"].lower()
                if coin_id in data:
                    current = data[coin_id]["usd"]
                    pred["outcome_24h"] = round(
                        (current - pred["price"]) / pred["price"] * 100, 2
                    )
                    updated = True
            except Exception:
                pass

        if updated:
            self.save_memory()
            self._update_weights()

    def _update_weights(self):
        """Adjust signal weights based on prediction accuracy."""
        recent = [p for p in self.memory["predictions"]
                  if p.get("outcome_24h") is not None][-20:]
        if len(recent) < 3:
            return

        signal_accuracy = {}
        for p in recent:
            sig = p["signal"]
            if sig not in signal_accuracy:
                signal_accuracy[sig] = []
            signal_accuracy[sig].append(p["outcome_24h"] > 0)

        for sig, outcomes in signal_accuracy.items():
            accuracy = sum(outcomes) / len(outcomes)
            if sig in self.memory["weights"]:
                old = self.memory["weights"][sig]
                self.memory["weights"][sig] = round(old * 0.8 + accuracy * 0.2, 3)

        self.memory["accuracy_history"].append({
            "timestamp": now_iso(),
            "weights": dict(self.memory["weights"]),
            "sample_size": len(recent),
        })
        self.save_memory()

    # ── Run (override in subclasses) ───────────────────────────────────
    def run(self, **kwargs) -> list[dict]:
        """Execute the agent. Must return a list of signal dicts."""
        raise NotImplementedError

    def execute(self, **kwargs) -> list[dict]:
        """Wrapper: run → publish to bus → record high-confidence predictions → learn."""
        print(f"  {self.emoji} {self.name} — scanning...")
        try:
            signals = self.run(**kwargs)
            if signals and self.publish_results:
                self.write_signals(signals)
                # High-confidence signals become predictions the team learns from
                for s in signals:
                    price = (s.get("details") or {}).get("price")
                    if s.get("confidence", 0) >= 0.6 and price:
                        self.record_prediction(s["coin"], s["signal"], s["confidence"], price)
            self.learn_from_outcomes()
            print(f"  {self.emoji} {self.name} — found {len(signals)} signal(s)")
            return signals
        except Exception as e:
            print(f"  {self.emoji} {self.name} — ERROR: {e}")
            return []
