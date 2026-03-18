"""
NEXUS TRADER - CoinSwitch Pro AI Trading Bot
Powered by Groq AI + CoinSwitch Pro API
All configuration via environment variables (Render-ready)
"""

import os, time, json, requests, threading, statistics
from datetime import datetime
from collections import deque
from cryptography.hazmat.primitives.asymmetric import ed25519
from urllib.parse import urlparse, urlencode
import urllib

try:
    from polymarket_engine import ArbScanner
    POLYMARKET_ENABLED = True
except ImportError:
    POLYMARKET_ENABLED = False

# ─── ALL KEYS FROM ENVIRONMENT VARIABLES ─────────────────
COINSWITCH_API_KEY    = os.environ.get("COINSWITCH_API_KEY", "")
COINSWITCH_SECRET_KEY = os.environ.get("COINSWITCH_SECRET_KEY", "")
GROQ_API_KEY          = os.environ.get("GROQ_API_KEY", "")

# ─── TRADING CONFIG (can also override via env) ───────────
BASE_URL              = "https://coinswitch.co"
GROQ_BASE_URL         = "https://api.groq.com/openai/v1"
TRADE_PAIRS           = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT"]
EXCHANGE              = os.environ.get("COINSWITCH_EXCHANGE", "c2c2")
MAX_TRADE_AMOUNT_USDT = float(os.environ.get("MAX_TRADE_USDT", "50"))
MIN_PROFIT_THRESHOLD  = 0.003
FEE_RATE              = 0.0009
SCAN_INTERVAL         = int(os.environ.get("SCAN_INTERVAL", "10"))
RSI_PERIOD            = 14
BOLLINGER_PERIOD      = 20


# ═══════════════════════════════════════════════════════════
# COINSWITCH API CLIENT
# ═══════════════════════════════════════════════════════════

class CoinSwitchClient:
    def __init__(self):
        self.api_key    = COINSWITCH_API_KEY
        self.secret_key = COINSWITCH_SECRET_KEY

    def _sign(self, method, endpoint, params=None, payload=None):
        epoch = str(int(time.time() * 1000))
        unquote_ep = endpoint
        if method == "GET" and params:
            ep2 = endpoint + ('&' if urlparse(endpoint).query else '?') + urlencode(params)
            unquote_ep = urllib.parse.unquote_plus(ep2)
            msg = method + unquote_ep + epoch
        elif method in ("POST", "DELETE") and payload:
            msg = method + endpoint + json.dumps(payload, separators=(',', ':'), sort_keys=True)
        else:
            msg = method + unquote_ep + epoch
        sk_bytes  = bytes.fromhex(self.secret_key)
        key       = ed25519.Ed25519PrivateKey.from_private_bytes(sk_bytes)
        sig       = key.sign(bytes(msg, 'utf-8')).hex()
        return sig, epoch

    def _req(self, method, endpoint, params=None, payload=None):
        if not self.api_key or not self.secret_key:
            return {"error": "API keys not configured"}
        try:
            sig, epoch = self._sign(method, endpoint, params, payload)
            headers = {
                'Content-Type': 'application/json',
                'X-AUTH-SIGNATURE': sig,
                'X-AUTH-APIKEY': self.api_key,
                'X-AUTH-EPOCH': epoch
            }
            url = BASE_URL + endpoint
            if method == "GET" and params:
                url += ('&' if urlparse(endpoint).query else '?') + urlencode(params)
            r = requests.request(method, url, headers=headers,
                                 json=payload if method != "GET" else None, timeout=10)
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def get_ticker_24hr(self, symbol):
        return self._req("GET", "/trade/api/v2/24hr/ticker", {"symbol": symbol, "exchange": EXCHANGE})

    def get_all_tickers(self):
        return self._req("GET", "/trade/api/v2/24hr/all-pairs/ticker", {"exchange": EXCHANGE})

    def get_depth(self, symbol):
        return self._req("GET", "/trade/api/v2/depth", {"exchange": EXCHANGE, "symbol": symbol})

    def get_portfolio(self):
        return self._req("GET", "/trade/api/v2/user/portfolio")

    def create_order(self, symbol, side, price, quantity):
        return self._req("POST", "/trade/api/v2/order", payload={
            "side": side, "symbol": symbol, "type": "limit",
            "price": round(float(price), 6), "quantity": round(float(quantity), 6),
            "exchange": EXCHANGE
        })

    def cancel_order(self, order_id):
        return self._req("DELETE", "/trade/api/v2/order", payload={"order_id": order_id})

    def get_open_orders(self):
        return self._req("GET", "/trade/api/v2/orders", {"open": "True", "exchanges": EXCHANGE})

    def validate_keys(self):
        return self._req("GET", "/trade/api/v2/validate/keys")


# ═══════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS
# ═══════════════════════════════════════════════════════════

class TA:
    @staticmethod
    def rsi(prices, period=RSI_PERIOD):
        if len(prices) < period + 1: return None
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains  = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]
        ag, al = sum(gains)/period, sum(losses)/period
        if al == 0: return 100
        return round(100 - (100 / (1 + ag/al)), 2)

    @staticmethod
    def bollinger(prices, period=BOLLINGER_PERIOD):
        if len(prices) < period: return None, None, None
        r = prices[-period:]
        mid = sum(r)/period
        std = statistics.stdev(r)
        return mid - 2*std, mid, mid + 2*std

    @staticmethod
    def ema(prices, period):
        if len(prices) < period: return None
        k, e = 2/(period+1), prices[0]
        for p in prices[1:]: e = p*k + e*(1-k)
        return e

    @staticmethod
    def trend(prices):
        if len(prices) < 10: return "NEUTRAL"
        r, o = sum(prices[-5:])/5, sum(prices[-10:-5])/5
        c = (r - o) / o
        return "BULLISH" if c > 0.005 else "BEARISH" if c < -0.005 else "NEUTRAL"


# ═══════════════════════════════════════════════════════════
# GROQ AI SIGNAL ENGINE
# ═══════════════════════════════════════════════════════════

class GroqEngine:
    def analyze(self, symbol, data):
        if not GROQ_API_KEY:
            return {"signal": "HOLD", "confidence": 0, "reason": "No Groq API key", "risk_level": "HIGH"}
        prompt = f"""Crypto trading analyst. Analyze {symbol} and give a trading signal.

Price: {data.get('current_price')} | 24h Change: {data.get('price_change_pct')}%
RSI: {data.get('rsi')} | Trend: {data.get('trend')} | BB Position: {data.get('bb_position')}
Spread: {data.get('spread_pct')}% | Volume: {data.get('volume')}

Respond ONLY with JSON:
{{"signal":"BUY"or"SELL"or"HOLD","confidence":0-100,"reason":"brief","risk_level":"LOW"or"MEDIUM"or"HIGH","suggested_entry":null_or_number,"suggested_target":null_or_number}}"""
        try:
            r = requests.post(f"{GROQ_BASE_URL}/chat/completions", headers={
                "Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"
            }, json={"model": "llama-3.3-70b-versatile", "messages": [{"role":"user","content":prompt}],
                     "max_tokens": 250, "temperature": 0.3}, timeout=15)
            text = r.json()["choices"][0]["message"]["content"].strip()
            for f in ["```json","```"]:
                if f in text: text = text.split(f)[1].split("```")[0].strip(); break
            return json.loads(text)
        except Exception as e:
            return {"signal":"HOLD","confidence":0,"reason":str(e),"risk_level":"HIGH"}


# ═══════════════════════════════════════════════════════════
# MAIN TRADING BOT
# ═══════════════════════════════════════════════════════════

class TradingBot:
    def __init__(self):
        self.client      = CoinSwitchClient()
        self.ai          = GroqEngine()
        self.ta          = TA()
        self.running     = False
        self.paper_mode  = True
        self.lock        = threading.Lock()
        self.trades_log  = []
        self.signals_log = []
        self.active_orders = {}
        self.arb_opportunities = {}
        self.price_history = {p: deque(maxlen=100) for p in TRADE_PAIRS}
        self.stats = {
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "total_pnl": 0.0, "scans": 0, "arb_opportunities_found": 0,
            "start_time": datetime.now().isoformat()
        }
        if POLYMARKET_ENABLED:
            self.arb_scanner = ArbScanner()
        else:
            self.arb_scanner = None

    def get_market_data(self, symbol):
        try:
            ticker = self.client.get_ticker_24hr(symbol)
            if "data" not in ticker or not ticker["data"]: return None
            t = list(ticker["data"].values())[0]
            price = float(t.get("lastPrice", 0))
            if price == 0: return None

            self.price_history[symbol].append(price)
            prices = list(self.price_history[symbol])

            rsi_val = self.ta.rsi(prices)
            bb_l, bb_m, bb_u = self.ta.bollinger(prices)
            trend = self.ta.trend(prices)
            bid, ask = float(t.get("bidPrice", 0)), float(t.get("askPrice", 0))
            spread = ((bid - ask) / ask * 100) if ask > 0 else 0
            bb_pos = round((price - bb_l) / (bb_u - bb_l), 2) if bb_l and bb_u and bb_u != bb_l else None

            return {
                "symbol": symbol, "current_price": price,
                "high_24h": float(t.get("highPrice", 0)), "low_24h": float(t.get("lowPrice", 0)),
                "volume": float(t.get("baseVolume", 0)), "price_change_pct": float(t.get("percentageChange", 0)),
                "bid_price": bid, "ask_price": ask, "spread_pct": round(spread, 4),
                "rsi": rsi_val, "trend": trend, "bb_position": bb_pos,
                "bb_lower": bb_l, "bb_upper": bb_u, "timestamp": datetime.now().isoformat()
            }
        except: return None

    def evaluate_signal(self, data):
        score, signals = 0, []
        rsi, bb_pos = data.get("rsi"), data.get("bb_position")
        trend = data.get("trend", "NEUTRAL")
        chg   = data.get("price_change_pct", 0)
        bid, ask = data.get("bid_price", 0), data.get("ask_price", 0)

        if rsi:
            if rsi < 30:   score += 2; signals.append("RSI_OVERSOLD")
            elif rsi < 40: score += 1; signals.append("RSI_BULLISH")
            elif rsi > 70: score -= 2; signals.append("RSI_OVERBOUGHT")
            elif rsi > 60: score -= 1; signals.append("RSI_BEARISH")
        if bb_pos is not None:
            if bb_pos < 0.1:   score += 2; signals.append("BB_LOWER_TOUCH")
            elif bb_pos < 0.25: score += 1; signals.append("BB_LOWER_ZONE")
            elif bb_pos > 0.9:  score -= 2; signals.append("BB_UPPER_TOUCH")
            elif bb_pos > 0.75: score -= 1; signals.append("BB_UPPER_ZONE")
        if trend == "BULLISH": score += 1; signals.append("TREND_UP")
        elif trend == "BEARISH": score -= 1; signals.append("TREND_DOWN")
        if chg > 3: score += 1; signals.append("MOMENTUM_UP")
        elif chg < -3: score -= 1; signals.append("MOMENTUM_DOWN")

        # Arbitrage detection
        if bid > 0 and ask > 0:
            spread = (bid - ask) / ask
            net = spread - (2 * FEE_RATE)
            if net > MIN_PROFIT_THRESHOLD:
                signals.append(f"ARB_{net:.3%}")
                score += 3

        ai = self.ai.analyze(data["symbol"], data)
        if ai.get("signal") == "BUY" and ai.get("confidence", 0) > 60:
            score += 2; signals.append(f"AI_BUY_{ai['confidence']}")
        elif ai.get("signal") == "SELL" and ai.get("confidence", 0) > 60:
            score -= 2; signals.append(f"AI_SELL_{ai['confidence']}")

        action = "BUY" if score >= 3 else "SELL" if score <= -3 else "HOLD"
        return {"action": action, "score": score, "signals": signals, "ai_signal": ai,
                "timestamp": datetime.now().isoformat()}

    def execute_trade(self, symbol, action, data):
        price = data["current_price"]
        qty   = (MAX_TRADE_AMOUNT_USDT / price)
        record = {
            "id": f"T{len(self.trades_log)+1:04d}", "symbol": symbol,
            "action": action, "price": price, "quantity": qty,
            "value_usdt": price * qty, "timestamp": datetime.now().isoformat(),
            "mode": "PAPER" if self.paper_mode else "LIVE", "status": "PENDING"
        }
        if self.paper_mode:
            record["status"] = "EXECUTED"; record["order_id"] = f"PAPER_{record['id']}"
        else:
            result = self.client.create_order(symbol, action.lower(), price, qty)
            if "data" in result:
                oid = result["data"].get("order_id")
                record["status"] = "OPEN"; record["order_id"] = oid
                self.active_orders[oid] = record
            else:
                record["status"] = "FAILED"; record["error"] = str(result)
        with self.lock:
            self.trades_log.append(record)
            self.stats["total_trades"] += 1
        return record

    def scan_markets(self):
        for symbol in TRADE_PAIRS:
            try:
                data = self.get_market_data(symbol)
                if not data: continue
                ev = self.evaluate_signal(data)
                entry = {
                    "symbol": symbol, "action": ev["action"], "score": ev["score"],
                    "signals": ev["signals"], "price": data["current_price"],
                    "rsi": data.get("rsi"), "trend": data.get("trend"),
                    "ai": ev.get("ai_signal", {}), "timestamp": datetime.now().isoformat()
                }
                with self.lock:
                    self.signals_log.insert(0, entry)
                    self.signals_log = self.signals_log[:200]
                    self.stats["scans"] += 1
                if ev["action"] in ("BUY", "SELL") and abs(ev["score"]) >= 3:
                    self.execute_trade(symbol, ev["action"], data)
            except Exception as e:
                print(f"  Error {symbol}: {e}")

        # Polymarket + Correlated Arb
        if self.arb_scanner:
            try:
                t = threading.Thread(target=self.arb_scanner.run_once, daemon=True)
                t.start()
                status = self.arb_scanner.get_status()
                with self.lock:
                    self.arb_opportunities = status
                    mp = status.get("mispricing_alerts", [])
                    ab = status.get("arb_alerts", [])
                    self.stats["arb_opportunities_found"] += len(mp) + len(ab)
                    for m in mp[:3]:
                        self.signals_log.insert(0, {
                            "symbol": "POLYMARKET", "action": "YES" if "YES" in m.get("direction","") else "HOLD",
                            "score": 5, "signals": [f"MISPRICED_{m.get('edge',0):.1%}", "POLY"],
                            "price": m.get("market_price", 0), "rsi": None, "trend": "POLY",
                            "ai": {"signal": m.get("direction","HOLD"), "confidence": m.get("confidence",0),
                                   "reason": m.get("reasoning",""), "risk_level": m.get("risk_level","MEDIUM")},
                            "poly_data": m, "timestamp": m.get("timestamp", datetime.now().isoformat())
                        })
                    for a in ab[:2]:
                        self.signals_log.insert(0, {
                            "symbol": "CORR_ARB", "action": "ARB", "score": 6,
                            "signals": [f"SPREAD_{a.get('current_spread',0):.1%}", a.get("description","")[:20]],
                            "price": a.get("price_a", 0), "rsi": None, "trend": "ARB",
                            "ai": {"signal": "ARB", "confidence": a.get("confidence",0),
                                   "reason": a.get("reasoning",""), "risk_level": "MEDIUM"},
                            "arb_data": a, "timestamp": a.get("timestamp", datetime.now().isoformat())
                        })
            except Exception as e:
                print(f"  Arb error: {e}")

    def run(self):
        self.running = True
        print(f"🤖 NEXUS TRADER started | mode={'PAPER' if self.paper_mode else 'LIVE'} | pairs={TRADE_PAIRS}")
        while self.running:
            try:
                self.scan_markets()
                time.sleep(SCAN_INTERVAL)
            except KeyboardInterrupt:
                self.running = False
            except Exception as e:
                print(f"Loop error: {e}"); time.sleep(10)

    def get_status(self):
        with self.lock:
            wr = (self.stats["winning_trades"] / max(self.stats["total_trades"], 1)) * 100
            return {
                "running": self.running, "mode": "PAPER" if self.paper_mode else "LIVE",
                "stats": {**self.stats, "win_rate": round(wr, 1)},
                "recent_signals": self.signals_log[:20],
                "recent_trades": self.trades_log[-20:],
                "active_orders": len(self.active_orders),
                "pairs": TRADE_PAIRS,
                "arb_opportunities": self.arb_opportunities,
                "keys_configured": bool(COINSWITCH_API_KEY and GROQ_API_KEY)
            }


bot = TradingBot()
