"""
NEXUS TRADER - CoinSwitch Pro AI Trading Bot
Powered by Groq AI + CoinSwitch Pro API

Signature logic follows official docs at https://api-trading.coinswitch.co
  GET:    msg = method + unquoted_endpoint_with_params + epoch_ms
  POST/DELETE: msg = method + endpoint + json.dumps(payload, sort_keys=True, separators)
  epoch always goes in X-AUTH-EPOCH header

Trade rules:
  - Minimum ₹100 per trade
  - ONE active trade at a time — waits for completion before next
  - Paper mode: simulated, no real orders
  - Live mode: real CoinSwitch orders
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

# ─── KEYS (Render environment variables) ──────────────────
COINSWITCH_API_KEY    = os.environ.get("COINSWITCH_API_KEY", "")
COINSWITCH_SECRET_KEY = os.environ.get("COINSWITCH_SECRET_KEY", "")
GROQ_API_KEY          = os.environ.get("GROQ_API_KEY", "")

# ─── TRADING CONFIG ───────────────────────────────────────
BASE_URL         = "https://coinswitch.co"
GROQ_BASE_URL    = "https://api.groq.com/openai/v1"

# Exchange: c2c2 = USDT pairs, coinswitchx = INR pairs
# Using coinswitchx for INR so ₹100 minimum maps directly
EXCHANGE         = os.environ.get("COINSWITCH_EXCHANGE", "coinswitchx")

# Trade pairs — INR pairs work directly with ₹ amounts
TRADE_PAIRS      = ["BTC/INR", "ETH/INR", "XRP/INR", "DOGE/INR", "MATIC/INR"]

# Minimum ₹100 per trade
MIN_TRADE_INR    = float(os.environ.get("MIN_TRADE_INR", "100"))
MAX_TRADE_INR    = float(os.environ.get("MAX_TRADE_INR", "4000"))   # ~$50

MIN_PROFIT_THRESHOLD = 0.003
FEE_RATE             = 0.0009
SCAN_INTERVAL        = int(os.environ.get("SCAN_INTERVAL", "10"))
ORDER_POLL_INTERVAL  = 5
ORDER_TIMEOUT        = 120
RSI_PERIOD           = 14
BOLLINGER_PERIOD     = 20


# ═══════════════════════════════════════════════════════════
# COINSWITCH API CLIENT
# Signature follows official docs exactly
# ═══════════════════════════════════════════════════════════

class CoinSwitchClient:
    def __init__(self):
        self.api_key    = COINSWITCH_API_KEY
        self.secret_key = COINSWITCH_SECRET_KEY
        self._precision_cache = {}

    def _sign(self, method, endpoint, params=None, payload=None):
        """
        Official signature logic:
        GET:         msg = METHOD + unquoted(endpoint + ?params) + epoch_ms
        POST/DELETE: msg = METHOD + endpoint + json_sorted_payload
        epoch always in X-AUTH-EPOCH header regardless
        """
        epoch = str(int(time.time() * 1000))

        if method == "GET":
            ep = endpoint
            if params:
                ep += ('&' if urlparse(endpoint).query else '?') + urlencode(params)
            unquoted = urllib.parse.unquote_plus(ep)
            msg = method + unquoted + epoch

        else:  # POST, DELETE
            body = json.dumps(payload, separators=(',', ':'), sort_keys=True) if payload else ""
            msg  = method + endpoint + body

        request_bytes  = bytes(msg, 'utf-8')
        secret_bytes   = bytes.fromhex(self.secret_key)
        private_key    = ed25519.Ed25519PrivateKey.from_private_bytes(secret_bytes)
        sig            = private_key.sign(request_bytes).hex()
        return sig, epoch

    def _req(self, method, endpoint, params=None, payload=None):
        if not self.api_key or not self.secret_key:
            return {"error": "API keys not configured"}
        try:
            sig, epoch = self._sign(method, endpoint, params, payload)
            headers = {
                'Content-Type':    'application/json',
                'X-AUTH-SIGNATURE': sig,
                'X-AUTH-APIKEY':   self.api_key,
                'X-AUTH-EPOCH':    epoch          # required on ALL requests
            }
            url = BASE_URL + endpoint
            if method == "GET" and params:
                url += ('&' if urlparse(endpoint).query else '?') + urlencode(params)

            r = requests.request(
                method, url, headers=headers,
                json=payload if method != "GET" else None,
                timeout=12
            )
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    # ── MARKET DATA ───────────────────────────────────────
    def get_server_time(self):
        """GET /trade/api/v2/time — no auth needed"""
        try:
            r = requests.get(f"{BASE_URL}/trade/api/v2/time",
                             headers={'Content-Type': 'application/json'}, timeout=5)
            return r.json()
        except:
            return {}

    def validate_keys(self):
        return self._req("GET", "/trade/api/v2/validate/keys")

    def get_ticker_24hr(self, symbol):
        return self._req("GET", "/trade/api/v2/24hr/ticker",
                         {"symbol": symbol, "exchange": EXCHANGE})

    def get_all_tickers(self):
        return self._req("GET", "/trade/api/v2/24hr/all-pairs/ticker",
                         {"exchange": EXCHANGE})

    def get_depth(self, symbol):
        return self._req("GET", "/trade/api/v2/depth",
                         {"exchange": EXCHANGE, "symbol": symbol})

    def get_trade_info(self, symbol):
        """GET /trade/api/v2/tradeInfo — returns min/max quote amounts and precision"""
        return self._req("GET", "/trade/api/v2/tradeInfo",
                         {"exchange": EXCHANGE, "symbol": symbol})

    def get_exchange_precision(self, symbol):
        """POST /trade/api/v2/exchangePrecision"""
        return self._req("POST", "/trade/api/v2/exchangePrecision",
                         payload={"exchange": EXCHANGE, "symbol": symbol})

    def get_precision(self, symbol):
        """Cache precision per symbol to avoid repeated calls"""
        if symbol in self._precision_cache:
            return self._precision_cache[symbol]
        result = self.get_trade_info(symbol)
        try:
            data     = result["data"][EXCHANGE][symbol]
            prec     = data.get("precision", {})
            base_prec  = prec.get("base", 5)
            quote_prec = prec.get("quote", 2)
            min_quote  = float(data.get("quote", {}).get("min", MIN_TRADE_INR))
            max_quote  = float(data.get("quote", {}).get("max", MAX_TRADE_INR))
            info = {"base": base_prec, "quote": quote_prec,
                    "min_quote": min_quote, "max_quote": max_quote}
        except:
            info = {"base": 5, "quote": 2,
                    "min_quote": MIN_TRADE_INR, "max_quote": MAX_TRADE_INR}
        self._precision_cache[symbol] = info
        return info

    def get_portfolio(self):
        return self._req("GET", "/trade/api/v2/user/portfolio")

    def get_trading_fee(self):
        return self._req("GET", "/trade/api/v2/tradingFee", {"exchange": EXCHANGE})

    # ── ORDERS ────────────────────────────────────────────
    def create_order(self, symbol, side, price, quantity):
        """
        POST /trade/api/v2/order
        price and quantity must respect exchange precision
        """
        prec = self.get_precision(symbol)
        payload = {
            "side":     side.lower(),
            "symbol":   symbol,
            "type":     "limit",
            "price":    round(float(price),    prec["quote"]),
            "quantity": round(float(quantity), prec["base"]),
            "exchange": EXCHANGE
        }
        print(f"  📤 Placing order: {payload}")
        return self._req("POST", "/trade/api/v2/order", payload=payload)

    def cancel_order(self, order_id):
        return self._req("DELETE", "/trade/api/v2/order",
                         payload={"order_id": order_id})

    def get_order(self, order_id):
        return self._req("GET", "/trade/api/v2/order",
                         params={"order_id": order_id})

    def get_open_orders(self):
        return self._req("GET", "/trade/api/v2/orders",
                         {"open": "True", "exchanges": EXCHANGE})

    def get_closed_orders(self, count=20):
        return self._req("GET", "/trade/api/v2/orders",
                         {"open": "False", "exchanges": EXCHANGE, "count": count})

    def get_active_coins(self):
        return self._req("GET", "/trade/api/v2/coins", {"exchange": EXCHANGE})


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
        r   = prices[-period:]
        mid = sum(r) / period
        std = statistics.stdev(r)
        return mid - 2*std, mid, mid + 2*std

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
            return {"signal": "HOLD", "confidence": 0,
                    "reason": "No Groq API key", "risk_level": "HIGH"}
        prompt = f"""Crypto trading analyst. Analyze {symbol} and give a signal.

Price: ₹{data.get('current_price')} | 24h Change: {data.get('price_change_pct')}%
RSI: {data.get('rsi')} | Trend: {data.get('trend')} | BB Position: {data.get('bb_position')}
Spread: {data.get('spread_pct')}% | Volume: {data.get('volume')}

Respond ONLY with JSON (no markdown):
{{"signal":"BUY"or"SELL"or"HOLD","confidence":0-100,"reason":"brief","risk_level":"LOW"or"MEDIUM"or"HIGH"}}"""
        try:
            r = requests.post(
                f"{GROQ_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile",
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 150, "temperature": 0.2},
                timeout=15
            )
            text = r.json()["choices"][0]["message"]["content"].strip()
            for fence in ["```json", "```"]:
                if fence in text:
                    text = text.split(fence)[1].split("```")[0].strip()
                    break
            return json.loads(text)
        except Exception as e:
            return {"signal": "HOLD", "confidence": 0,
                    "reason": str(e)[:80], "risk_level": "HIGH"}


# ═══════════════════════════════════════════════════════════
# MAIN TRADING BOT
# ═══════════════════════════════════════════════════════════

class TradingBot:
    def __init__(self):
        self.client       = CoinSwitchClient()
        self.ai           = GroqEngine()
        self.ta           = TA()
        self.running      = False
        self.paper_mode   = True
        self.lock         = threading.Lock()
        self.trade_lock   = threading.Lock()

        self.trades_log       = []
        self.signals_log      = []
        self.active_orders    = {}
        self.arb_opportunities = {}
        self.price_history    = {p: deque(maxlen=100) for p in TRADE_PAIRS}

        # ONE TRADE AT A TIME
        self.current_trade = None

        self.stats = {
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "total_pnl_inr": 0.0, "scans": 0, "arb_opportunities_found": 0,
            "start_time": datetime.now().isoformat(),
            "current_trade_status": "IDLE",
            "min_trade_inr": MIN_TRADE_INR,
            "max_trade_inr": MAX_TRADE_INR,
            "exchange": EXCHANGE,
        }

        if POLYMARKET_ENABLED:
            self.arb_scanner = ArbScanner()
        else:
            self.arb_scanner = None

    # ── TRADE SLOT ────────────────────────────────────────
    def is_trade_slot_free(self):
        with self.trade_lock:
            return self.current_trade is None

    def _set_current_trade(self, trade):
        with self.trade_lock:
            self.current_trade = trade
        with self.lock:
            self.stats["current_trade_status"] = trade["status"] if trade else "IDLE"

    # ── ORDER MONITOR ─────────────────────────────────────
    def _monitor_order(self, trade):
        """Poll order status until EXECUTED/CANCELLED/EXPIRED, then free slot."""
        order_id = trade.get("order_id", "")
        started  = time.time()
        print(f"  👁  Monitoring {order_id} ({trade['symbol']})...")

        while self.running:
            time.sleep(ORDER_POLL_INTERVAL)

            if time.time() - started > ORDER_TIMEOUT:
                print(f"  ⏱  Order {order_id} timed out — cancelling")
                self.client.cancel_order(order_id)
                self._finish_trade(trade, "CANCELLED", 0)
                return

            result = self.client.get_order(order_id)
            if "error" in result:
                print(f"  ⚠️  Poll error: {result['error']}")
                continue

            od     = result.get("data", {})
            status = od.get("status", "OPEN")
            exec_q = float(od.get("executed_qty",  0))
            avg_p  = float(od.get("average_price", 0) or 0)

            trade["exchange_status"] = status
            trade["executed_qty"]    = exec_q
            trade["average_price"]   = avg_p

            print(f"  📊 {trade['symbol']} {status} | exec: {exec_q} @ avg ₹{avg_p}")

            terminal = {"EXECUTED", "CANCELLED", "EXPIRED",
                        "DISCARDED", "CANCELLATION_RAISED", "EXPIRATION_RAISED"}
            if status in terminal:
                pnl = 0.0
                if status == "EXECUTED" and avg_p > 0 and trade["action"] == "SELL":
                    pnl = (avg_p - trade["price"]) * exec_q
                self._finish_trade(trade, status, pnl)
                return

    def _finish_trade(self, trade, final_status, pnl_inr):
        trade["status"]      = final_status
        trade["final_pnl"]   = round(pnl_inr, 2)
        trade["finished_at"] = datetime.now().isoformat()

        with self.lock:
            self.stats["total_pnl_inr"] += pnl_inr
            if pnl_inr > 0:  self.stats["winning_trades"] += 1
            elif pnl_inr < 0: self.stats["losing_trades"]  += 1

        self._set_current_trade(None)
        print(f"  ✅ Trade {trade['id']} done: {final_status} | P&L: ₹{pnl_inr:+.2f}")

    # ── MARKET DATA ───────────────────────────────────────
    def get_market_data(self, symbol):
        try:
            ticker = self.client.get_ticker_24hr(symbol)
            if "data" not in ticker or not ticker["data"]:
                return None
            t     = list(ticker["data"].values())[0]
            price = float(t.get("lastPrice", 0))
            if price == 0: return None

            self.price_history[symbol].append(price)
            prices = list(self.price_history[symbol])

            rsi_val          = self.ta.rsi(prices)
            bb_l, bb_m, bb_u = self.ta.bollinger(prices)
            trend            = self.ta.trend(prices)
            bid  = float(t.get("bidPrice", 0))
            ask  = float(t.get("askPrice", 0))
            spread = ((bid - ask) / ask * 100) if ask > 0 else 0
            bb_pos = round((price - bb_l) / (bb_u - bb_l), 2) \
                     if bb_l and bb_u and bb_u != bb_l else None

            return {
                "symbol":           symbol,
                "current_price":    price,
                "high_24h":         float(t.get("highPrice", 0)),
                "low_24h":          float(t.get("lowPrice", 0)),
                "volume":           float(t.get("baseVolume", 0)),
                "price_change_pct": float(t.get("percentageChange", 0)),
                "bid_price":        bid,
                "ask_price":        ask,
                "spread_pct":       round(spread, 4),
                "rsi":              rsi_val,
                "trend":            trend,
                "bb_position":      bb_pos,
                "bb_lower":         bb_l,
                "bb_upper":         bb_u,
                "timestamp":        datetime.now().isoformat()
            }
        except Exception as e:
            print(f"  Market data error {symbol}: {e}")
            return None

    # ── SIGNAL EVALUATION ─────────────────────────────────
    def evaluate_signal(self, data):
        score, signals = 0, []
        rsi    = data.get("rsi")
        bb_pos = data.get("bb_position")
        trend  = data.get("trend", "NEUTRAL")
        chg    = data.get("price_change_pct", 0)
        bid    = data.get("bid_price", 0)
        ask    = data.get("ask_price", 0)

        if rsi:
            if rsi < 30:    score += 2; signals.append("RSI_OVERSOLD")
            elif rsi < 40:  score += 1; signals.append("RSI_BULLISH")
            elif rsi > 70:  score -= 2; signals.append("RSI_OVERBOUGHT")
            elif rsi > 60:  score -= 1; signals.append("RSI_BEARISH")

        if bb_pos is not None:
            if bb_pos < 0.10:   score += 2; signals.append("BB_LOWER_TOUCH")
            elif bb_pos < 0.25: score += 1; signals.append("BB_LOWER_ZONE")
            elif bb_pos > 0.90: score -= 2; signals.append("BB_UPPER_TOUCH")
            elif bb_pos > 0.75: score -= 1; signals.append("BB_UPPER_ZONE")

        if trend == "BULLISH":  score += 1; signals.append("TREND_UP")
        elif trend == "BEARISH": score -= 1; signals.append("TREND_DOWN")

        if chg > 3:   score += 1; signals.append("MOMENTUM_UP")
        elif chg < -3: score -= 1; signals.append("MOMENTUM_DOWN")

        if bid > 0 and ask > 0:
            spread = (bid - ask) / ask
            net    = spread - (2 * FEE_RATE)
            if net > MIN_PROFIT_THRESHOLD:
                signals.append(f"ARB_{net:.3%}"); score += 3

        ai = self.ai.analyze(data["symbol"], data)
        if ai.get("signal") == "BUY" and ai.get("confidence", 0) > 60:
            score += 2; signals.append(f"AI_BUY_{ai['confidence']}")
        elif ai.get("signal") == "SELL" and ai.get("confidence", 0) > 60:
            score -= 2; signals.append(f"AI_SELL_{ai['confidence']}")

        action = "BUY" if score >= 3 else "SELL" if score <= -3 else "HOLD"
        return {"action": action, "score": score, "signals": signals,
                "ai_signal": ai, "timestamp": datetime.now().isoformat()}

    # ── EXECUTE TRADE ─────────────────────────────────────
    def execute_trade(self, symbol, action, data):
        """
        Place a single trade.
        Uses exchange precision from tradeInfo API.
        Minimum ₹100, maximum MAX_TRADE_INR.
        """
        price = data["current_price"]
        prec  = self.client.get_precision(symbol)

        # Clamp trade amount to [min_quote, max_quote] from exchange AND our config
        trade_inr = max(
            max(MIN_TRADE_INR, prec["min_quote"]),
            min(MAX_TRADE_INR, prec["max_quote"])
        )
        # Quantity in base coin
        quantity = trade_inr / price

        record = {
            "id":             f"T{len(self.trades_log)+1:04d}",
            "symbol":         symbol,
            "action":         action,
            "price":          price,
            "quantity":       round(quantity, prec["base"]),
            "value_inr":      round(trade_inr, 2),
            "timestamp":      datetime.now().isoformat(),
            "mode":           "PAPER" if self.paper_mode else "LIVE",
            "status":         "PENDING",
            "exchange_status": None,
            "executed_qty":   0,
            "average_price":  0,
            "final_pnl":      0,
        }

        emoji = "📝" if self.paper_mode else "💸"
        print(f"\n  {emoji} {'PAPER' if self.paper_mode else 'LIVE'} "
              f"{action} {record['quantity']} {symbol} @ ₹{price:,.2f} = ₹{trade_inr:.0f}")

        if self.paper_mode:
            import random
            record["status"]         = "EXECUTED"
            record["order_id"]       = f"PAPER_{record['id']}"
            record["exchange_status"] = "EXECUTED"
            pnl = trade_inr * random.uniform(-0.018, 0.022)
            record["final_pnl"] = round(pnl, 2)

            with self.lock:
                self.trades_log.append(record)
                self.stats["total_trades"]   += 1
                self.stats["total_pnl_inr"]  += pnl
                if pnl > 0: self.stats["winning_trades"] += 1
                else:       self.stats["losing_trades"]  += 1

            # Paper: free slot immediately
            self._set_current_trade(None)

        else:
            # LIVE: place real order
            result = self.client.create_order(
                symbol, action, price, record["quantity"]
            )
            if "data" in result:
                oid = result["data"].get("order_id")
                record["status"]   = "OPEN"
                record["order_id"] = oid
                self.active_orders[oid] = record

                with self.lock:
                    self.trades_log.append(record)
                    self.stats["total_trades"] += 1

                self._set_current_trade(record)

                # Background thread monitors until done
                t = threading.Thread(
                    target=self._monitor_order, args=(record,), daemon=True
                )
                t.start()
            else:
                err = result.get("error") or result.get("message") or str(result)
                print(f"  ❌ Order failed: {err}")
                record["status"] = "FAILED"
                record["error"]  = err
                with self.lock:
                    self.trades_log.append(record)
                self._set_current_trade(None)

        return record

    # ── MARKET SCAN ───────────────────────────────────────
    def scan_markets(self):
        for symbol in TRADE_PAIRS:
            try:
                data = self.get_market_data(symbol)
                if not data: continue

                ev = self.evaluate_signal(data)

                entry = {
                    "symbol":    symbol,
                    "action":    ev["action"],
                    "score":     ev["score"],
                    "signals":   ev["signals"],
                    "price":     data["current_price"],
                    "rsi":       data.get("rsi"),
                    "trend":     data.get("trend"),
                    "ai":        ev.get("ai_signal", {}),
                    "timestamp": datetime.now().isoformat()
                }
                with self.lock:
                    self.signals_log.insert(0, entry)
                    self.signals_log = self.signals_log[:200]
                    self.stats["scans"] += 1

                # Trade gate: strong signal AND slot is free
                if ev["action"] in ("BUY", "SELL") and abs(ev["score"]) >= 3:
                    if self.is_trade_slot_free():
                        self._set_current_trade({"symbol": symbol,
                                                  "status": "PLACING",
                                                  "id": "..."})
                        self.execute_trade(symbol, ev["action"], data)
                    else:
                        ct = self.current_trade or {}
                        print(f"  ⏳ Skip {symbol} — waiting for "
                              f"{ct.get('symbol','?')} ({ct.get('status','?')})")

            except Exception as e:
                print(f"  Error scanning {symbol}: {e}")

        # Polymarket arb
        if self.arb_scanner:
            try:
                t = threading.Thread(
                    target=self.arb_scanner.run_once, daemon=True
                )
                t.start()
                status = self.arb_scanner.get_status()
                with self.lock:
                    self.arb_opportunities = status
                    mp = status.get("mispricing_alerts", [])
                    ab = status.get("arb_alerts", [])
                    self.stats["arb_opportunities_found"] += len(mp) + len(ab)
                    for m in mp[:3]:
                        self.signals_log.insert(0, {
                            "symbol": "POLYMARKET",
                            "action": "YES" if "YES" in m.get("direction","") else "HOLD",
                            "score": 5,
                            "signals": [f"MISPRICED_{m.get('edge',0):.1%}", "POLY"],
                            "price": m.get("market_price", 0),
                            "rsi": None, "trend": "POLY",
                            "ai": {"signal": m.get("direction","HOLD"),
                                   "confidence": m.get("confidence",0),
                                   "reason": m.get("reasoning",""),
                                   "risk_level": m.get("risk_level","MEDIUM")},
                            "poly_data": m,
                            "timestamp": m.get("timestamp", datetime.now().isoformat())
                        })
                    for a in ab[:2]:
                        self.signals_log.insert(0, {
                            "symbol": "CORR_ARB", "action": "ARB", "score": 6,
                            "signals": [f"SPREAD_{a.get('current_spread',0):.1%}",
                                        a.get("description","")[:20]],
                            "price": a.get("price_a", 0),
                            "rsi": None, "trend": "ARB",
                            "ai": {"signal": "ARB",
                                   "confidence": a.get("confidence",0),
                                   "reason": a.get("reasoning",""),
                                   "risk_level": "MEDIUM"},
                            "arb_data": a,
                            "timestamp": a.get("timestamp", datetime.now().isoformat())
                        })
            except Exception as e:
                print(f"  Arb error: {e}")

    # ── MAIN LOOP ─────────────────────────────────────────
    def run(self):
        self.running = True
        print(f"\n{'='*60}")
        print(f"🤖 NEXUS TRADER — {'PAPER' if self.paper_mode else 'LIVE'} MODE")
        print(f"   Exchange:  {EXCHANGE}")
        print(f"   Pairs:     {', '.join(TRADE_PAIRS)}")
        print(f"   Min trade: ₹{MIN_TRADE_INR}")
        print(f"   Max trade: ₹{MAX_TRADE_INR}")
        print(f"   Slot:      ONE TRADE AT A TIME")
        print(f"{'='*60}\n")

        while self.running:
            try:
                self.scan_markets()
                time.sleep(SCAN_INTERVAL)
            except KeyboardInterrupt:
                self.running = False
            except Exception as e:
                print(f"Loop error: {e}"); time.sleep(10)

    # ── STATUS ────────────────────────────────────────────
    def get_status(self):
        with self.lock:
            wins   = self.stats["winning_trades"]
            total  = self.stats["total_trades"]
            wr     = (wins / max(total, 1)) * 100
            ct     = self.current_trade
            return {
                "running":        self.running,
                "mode":           "PAPER" if self.paper_mode else "LIVE",
                "stats":          {**self.stats, "win_rate": round(wr, 1),
                                   "total_pnl": self.stats["total_pnl_inr"]},
                "recent_signals": self.signals_log[:20],
                "recent_trades":  self.trades_log[-20:],
                "active_orders":  len(self.active_orders),
                "pairs":          TRADE_PAIRS,
                "arb_opportunities": self.arb_opportunities,
                "keys_configured":   bool(COINSWITCH_API_KEY and GROQ_API_KEY),
                "current_trade": {
                    "active":    ct is not None,
                    "id":        ct.get("id")        if ct else None,
                    "symbol":    ct.get("symbol")    if ct else None,
                    "action":    ct.get("action")    if ct else None,
                    "status":    ct.get("status")    if ct else None,
                    "value_inr": ct.get("value_inr") if ct else None,
                },
                "trade_slot": "OCCUPIED" if ct else "FREE",
            }


bot = TradingBot()
