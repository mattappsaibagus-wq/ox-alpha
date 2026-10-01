"""
InvestmentAdvisor — reads every signal on the bus plus due-diligence scores,
applies the user's portfolio context, and writes a scored BUY/WATCH/AVOID
report with a suggested position size.
"""
import json
import os
from .base import BaseAgent, now_jst, REPORTS_DIR, SIGNALS_FILE, MEMORY_DIR


class InvestmentAdvisor(BaseAgent):
    name = "advisor"
    emoji = "💰"
    publish_results = False  # verdicts are derived output — report only, not raw signals

    def default_weights(self):
        return {
            "microcap_opportunity": 0.40,
            "whale_up": 0.30,
            "whale_down": 0.30,   # negative weight for down-moves
            "news_event": 0.20,
            "dd_result": 0.50,    # gate: DD score scales everything
        }

    def run(self, **kwargs):
        signals = self.read_signals()
        portfolio = self._load_watchlist()
        out = self._score_all(signals, portfolio)
        self._write_report(out)
        return out

    def _load_watchlist(self) -> dict:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "watchlist.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {"holdings": [], "watchlist": []}

    def _score_all(self, signals, portfolio) -> list[dict]:
        # Accumulate per-coin scores from the bus
        agg = {}
        for sig in signals:
            coin = sig.get("coin", "").upper()
            if not coin:
                continue
            bucket = agg.setdefault(coin, {"bias": 0.0, "count": 0, "notes": [], "price": 0})
            if sig["signal"] == "dd_result":
                bucket["dd"] = sig["confidence"]
            else:
                price = (sig.get("details") or {}).get("price") or 0
                if price and not bucket["price"]:
                    bucket["price"] = price
                w = self.memory["weights"].get(sig["signal"], self.default_weights().get(sig["signal"], 0.1))
                if sig["signal"] == "whale_down":
                    w = -abs(w)
                bias = sig["confidence"] * w
                bucket["bias"] += bias
                bucket["count"] += 1
                if sig.get("details", {}).get("reasons"):
                    bucket["notes"] += sig["details"]["reasons"][:3]

        holdings = {h["symbol"].upper() for h in portfolio.get("holdings", [])}
        watch = {c.upper() for c in portfolio.get("watchlist", [])}

        verdicts = []
        for coin, b in agg.items():
            has_dd = "dd" in b
            dd_score = b.get("dd", 0.0)

            base = b["bias"] / max(b["count"], 1)
            # DD is the gate: verified coins scale by their DD score,
            # unverified coins are capped so they can not reach BUY.
            if has_dd:
                score = base * (0.4 + 0.6 * dd_score)
            else:
                score = base * 0.35   # awaiting verification
                dd_score = 0.0

            if score >= 0.15 and has_dd and dd_score >= 0.4:
                action = "BUY"
                if b.get("price", 0) > 0:
                    self.record_prediction(coin, "advisor_buy", score, b["price"])
            elif score >= 0.0:
                action = "WATCH"
            else:
                action = "AVOID"

            verdicts.append({
                "coin": coin,
                "action": action,
                "score": round(score, 3),
                "base_bias": round(base, 3),
                "signals": b["count"],
                "dd": b.get("dd"),
                "pending_dd": not has_dd,
                "in_portfolio": coin in holdings,
                "on_watchlist": coin in watch,
                "reasons": b["notes"][:5],
                "suggested_size_pct": self._suggest_size(action, coin, holdings),
            })

        verdicts.sort(key=lambda v: v["score"], reverse=True)
        return verdicts

    def _suggest_size(self, action: str, coin: str, holdings) -> float:
        """% of a hypothetical portfolio. Position sizing logic (team knowledge)."""
        if action != "BUY":
            return 0.0
        if coin in {"BTC", "ETH"}:
            return 5.0
        if coin in holdings:
            return 2.0
        return 1.0

    def _write_report(self, verdicts):
        now = now_jst()
        ts = now.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(REPORTS_DIR, f"report_{ts}.md")
        with open(path, "w") as f:
            f.write("# 🧠 Crypto Agent Team — Investment Report\n\n")
            f.write(f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S')} JST\n\n")
            summary = {"BUY": 0, "WATCH": 0, "AVOID": 0}
            for v in verdicts:
                summary[v["action"]] += 1
            f.write("| Action | Count |\n|--------|-------|\n")
            for k in ("BUY", "WATCH", "AVOID"):
                f.write(f"| {k} | {summary[k]} |\n")
            f.write("\n---\n\n")
            for v in verdicts:
                emoji = {"BUY": "🟢", "WATCH": "🟡", "AVOID": "🔴"}[v["action"]]
                f.write(f"## {emoji} {v['coin']} — {v['action']}\n\n")
                f.write(f"- **Score:** {v['score']}\n")
                f.write(f"- **Signals:** {v['signals']} | **DD score:** {v['dd'] or 'n/a'}\n")
                if v["in_portfolio"]:
                    f.write("- **Status:** already in your holdings\n")
                elif v["on_watchlist"]:
                    f.write("- **Status:** on your watchlist\n")
                if v.get("pending_dd"):
                    f.write("- **Flag:** ⚠ due diligence not yet run on this coin — treat as provisional\n")
                if v["suggested_size_pct"]:
                    f.write(f"- **Suggested size:** up to {v['suggested_size_pct']}% of portfolio\n")
                if v["reasons"]:
                    f.write(f"- **Why:** {'; '.join(v['reasons'])}\n")
                f.write("\n")

                for pred_sig in self.memory["predictions"][-3:]:
                    if pred_sig["coin"] == v["coin"] and pred_sig.get("outcome_24h") is not None:
                        f.write(f"  *learned: prior {pred_sig['signal']} call → {pred_sig['outcome_24h']:+.2f}% in 24h*\n")
                        break
        print(f"  💰 Report written → data/reports/report_{ts}.md")