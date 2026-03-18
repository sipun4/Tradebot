"""
POLYMARKET MISPRICING + CORRELATED ARBITRAGE ENGINE
All keys read from environment variables
"""

import os, time, json, requests, threading
from datetime import datetime

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

MIN_MISPRICING_EDGE   = 0.07
MIN_ARB_SPREAD        = 0.04
MAX_MARKET_STAKE_USDT = float(os.environ.get("MAX_TRADE_USDT", "20"))
SCAN_INTERVAL_SEC     = 15

CORRELATION_PAIRS = [
    ("will-btc-hit-100k",     "will-eth-hit-5k",         0.85, "BTC/ETH price targets"),
    ("trump-win-2024",         "republican-senate-2024",  0.80, "US election sweep"),
    ("fed-rate-cut-sept",      "btc-above-70k-sept",      0.65, "Rate cut crypto pump"),
]


class PolymarketClient:
    GAMMA = "https://gamma-api.polymarket.com"

    def get_active_markets(self, limit=30):
        try:
            r = requests.get(f"{self.GAMMA}/markets", params={
                "active": "true", "closed": "false", "limit": limit,
                "order": "volume24hr", "ascending": "false"
            }, timeout=10)
            return r.json() if r.status_code == 200 else []
        except Exception as e:
            print(f"  [Polymarket] {e}"); return []

    def get_market_by_slug(self, slug):
        try:
            r = requests.get(f"{self.GAMMA}/markets", params={"slug": slug}, timeout=10)
            d = r.json()
            return d[0] if isinstance(d, list) and d else None
        except: return None

    def search_markets(self, query, limit=4):
        try:
            r = requests.get(f"{self.GAMMA}/markets", params={
                "q": query, "active": "true", "limit": limit
            }, timeout=10)
            return r.json() if r.status_code == 200 else []
        except: return []


class GroqProbabilityEngine:
    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }

    def _call(self, prompt):
        if not GROQ_API_KEY:
            raise ValueError("GROQ_API_KEY not set")
        r = requests.post(GROQ_URL, headers=self.headers, json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400, "temperature": 0.2
        }, timeout=20)
        text = r.json()["choices"][0]["message"]["content"].strip()
        for fence in ["```json", "```"]:
            if fence in text:
                text = text.split(fence)[1].split("```")[0].strip(); break
        return json.loads(text)

    def calculate_true_probability(self, market):
        question  = market.get("question", "Unknown")
        yes_price = float((market.get("outcomePrices") or ["0.5"])[0])
        end_date  = market.get("endDate", "Unknown")
        volume    = market.get("volume", 0)
        desc      = (market.get("description") or "")[:300]

        prompt = f"""You are a professional prediction market analyst.

Event: {question}
Market YES probability: {yes_price:.1%}
End date: {end_date} | Volume: ${volume}
Description: {desc}

Calculate the TRUE probability using base rates, current knowledge, and statistical reasoning.

Respond ONLY in JSON:
{{
  "true_probability": 0.XX,
  "market_probability": {yes_price:.4f},
  "edge": 0.XX,
  "direction": "BUY_YES" or "BUY_NO" or "NO_EDGE",
  "confidence": 0-100,
  "reasoning": "2-3 sentence explanation",
  "key_factors": ["factor1", "factor2"],
  "risk_level": "LOW" or "MEDIUM" or "HIGH"
}}"""
        try:
            r = self._call(prompt)
            r.update({"market_question": question, "market_id": market.get("id"),
                      "end_date": end_date, "volume": volume,
                      "timestamp": datetime.now().isoformat()})
            return r
        except Exception as e:
            return {"true_probability": yes_price, "market_probability": yes_price,
                    "edge": 0, "direction": "NO_EDGE", "confidence": 0,
                    "reasoning": f"Error: {e}", "key_factors": [], "risk_level": "HIGH",
                    "market_question": question, "timestamp": datetime.now().isoformat()}

    def analyze_correlation(self, ma, mb, expected_corr, description):
        pa = float((ma.get("outcomePrices") or ["0.5"])[0])
        pb = float((mb.get("outcomePrices") or ["0.5"])[0])
        spread = abs(pa - pb)

        prompt = f"""Quantitative analyst specializing in prediction market arbitrage.

Market A: {ma.get('question','N/A')} | YES: {pa:.2%}
Market B: {mb.get('question','N/A')} | YES: {pb:.2%}
Historical correlation: {expected_corr:.0%} | Relationship: {description}
Current spread: {spread:.2%}

Is this an arbitrage opportunity?

Respond ONLY in JSON:
{{
  "is_arbitrage": true or false,
  "overpriced_market": "A" or "B" or "NONE",
  "underpriced_market": "A" or "B" or "NONE",
  "arb_edge": 0.XX,
  "action": "BUY_A_SELL_B" or "BUY_B_SELL_A" or "HOLD",
  "confidence": 0-100,
  "reasoning": "2-3 sentence explanation",
  "convergence_timeframe": "hours" or "days" or "weeks"
}}"""
        try:
            r = self._call(prompt)
            r.update({"market_a": ma.get("question","N/A"), "market_b": mb.get("question","N/A"),
                      "price_a": pa, "price_b": pb, "current_spread": spread,
                      "timestamp": datetime.now().isoformat()})
            return r
        except Exception as e:
            return {"is_arbitrage": False, "action": "HOLD", "arb_edge": 0, "confidence": 0,
                    "reasoning": f"Error: {e}", "timestamp": datetime.now().isoformat()}


class ArbScanner:
    def __init__(self):
        self.poly    = PolymarketClient()
        self.ai      = GroqProbabilityEngine()
        self.running = False
        self.lock    = threading.Lock()
        self.mispricing_alerts = []
        self.arb_alerts        = []
        self.scanned_markets   = []
        self.stats = {"markets_scanned": 0, "mispricing_found": 0,
                      "arb_pairs_found": 0, "total_edge_usdt": 0.0, "last_scan": None}

    def scan_mispriced_markets(self):
        markets = self.poly.get_active_markets(limit=30)
        results = []
        for market in markets[:15]:
            try:
                if not market.get("active"): continue
                volume = float(market.get("volume") or 0)
                if volume < 1000: continue
                prices = market.get("outcomePrices")
                if not prices or len(prices) < 2: continue
                yes_price = float(prices[0])
                if yes_price < 0.05 or yes_price > 0.95: continue

                analysis = self.ai.calculate_true_probability(market)
                edge = abs(analysis.get("edge") or 0)
                result = {
                    "market_id": market.get("id"), "question": market.get("question","N/A"),
                    "market_price": yes_price, "ai_probability": analysis.get("true_probability", yes_price),
                    "edge": edge, "direction": analysis.get("direction","NO_EDGE"),
                    "confidence": analysis.get("confidence", 0), "reasoning": analysis.get("reasoning",""),
                    "key_factors": analysis.get("key_factors",[]), "risk_level": analysis.get("risk_level","HIGH"),
                    "volume": volume, "end_date": market.get("endDate","N/A"),
                    "timestamp": datetime.now().isoformat(),
                    "is_opportunity": edge >= MIN_MISPRICING_EDGE and analysis.get("confidence",0) >= 55
                }
                results.append(result)
                with self.lock:
                    self.stats["markets_scanned"] += 1
                    if result["is_opportunity"]:
                        self.stats["mispricing_found"] += 1
                        self.mispricing_alerts.insert(0, result)
                        self.mispricing_alerts = self.mispricing_alerts[:50]
                        self.stats["total_edge_usdt"] += edge * MAX_MARKET_STAKE_USDT
                time.sleep(1.5)
            except Exception as e:
                print(f"  [Polymarket] {e}")
        with self.lock:
            self.scanned_markets = (results + self.scanned_markets)[:200]
            self.stats["last_scan"] = datetime.now().isoformat()

    def scan_correlated_arb(self):
        for slug_a, slug_b, corr, desc in CORRELATION_PAIRS:
            try:
                ma = self.poly.get_market_by_slug(slug_a)
                mb = self.poly.get_market_by_slug(slug_b)
                if not ma or not mb: continue
                pa = float((ma.get("outcomePrices") or ["0.5"])[0])
                pb = float((mb.get("outcomePrices") or ["0.5"])[0])
                if abs(pa - pb) < MIN_ARB_SPREAD: continue
                analysis = self.ai.analyze_correlation(ma, mb, corr, desc)
                if analysis.get("is_arbitrage") and analysis.get("confidence",0) >= 55:
                    alert = {
                        "description": desc, "market_a": ma.get("question", slug_a),
                        "market_b": mb.get("question", slug_b), "price_a": pa, "price_b": pb,
                        "current_spread": abs(pa-pb), "arb_edge": analysis.get("arb_edge",0),
                        "action": analysis.get("action","HOLD"), "confidence": analysis.get("confidence",0),
                        "reasoning": analysis.get("reasoning",""),
                        "convergence": analysis.get("convergence_timeframe","?"),
                        "expected_corr": corr, "timestamp": datetime.now().isoformat()
                    }
                    with self.lock:
                        self.arb_alerts.insert(0, alert)
                        self.arb_alerts = self.arb_alerts[:50]
                        self.stats["arb_pairs_found"] += 1
                time.sleep(2)
            except Exception as e:
                print(f"  [Arb] {e}")

    def find_dynamic_crypto_pairs(self):
        try:
            btc = self.poly.search_markets("bitcoin price", limit=3)
            eth = self.poly.search_markets("ethereum price", limit=3)
            for bm in btc[:2]:
                for em in eth[:2]:
                    pa = float((bm.get("outcomePrices") or ["0.5"])[0])
                    pb = float((em.get("outcomePrices") or ["0.5"])[0])
                    if abs(pa - pb) > 0.15:
                        analysis = self.ai.analyze_correlation(bm, em, 0.80, "BTC/ETH correlation")
                        if analysis.get("is_arbitrage"):
                            alert = {
                                "description": "Dynamic BTC/ETH Correlation",
                                "market_a": bm.get("question","BTC"), "market_b": em.get("question","ETH"),
                                "price_a": pa, "price_b": pb, "current_spread": abs(pa-pb),
                                "arb_edge": analysis.get("arb_edge",0), "action": analysis.get("action","HOLD"),
                                "confidence": analysis.get("confidence",0), "reasoning": analysis.get("reasoning",""),
                                "convergence": analysis.get("convergence_timeframe","days"),
                                "expected_corr": 0.80, "timestamp": datetime.now().isoformat()
                            }
                            with self.lock:
                                self.arb_alerts.insert(0, alert)
                                self.stats["arb_pairs_found"] += 1
                    time.sleep(1)
        except Exception as e:
            print(f"  [Dynamic] {e}")

    def get_status(self):
        with self.lock:
            return {
                "stats": self.stats,
                "mispricing_alerts": self.mispricing_alerts[:20],
                "arb_alerts": self.arb_alerts[:20],
                "scanned_markets": self.scanned_markets[:50]
            }

    def run_once(self):
        self.scan_mispriced_markets()
        self.scan_correlated_arb()
        self.find_dynamic_crypto_pairs()

    def run_loop(self):
        self.running = True
        while self.running:
            try:
                self.run_once()
                time.sleep(SCAN_INTERVAL_SEC)
            except Exception as e:
                print(f"  [Scanner] {e}"); time.sleep(30)
