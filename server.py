"""
NEXUS TRADER - Flask Server (Render-ready)
- Serves the dashboard HTML at /
- Provides all API endpoints at /api/...
- Auto-starts the trading bot on launch
- All config via environment variables
"""

import os, threading
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from bot import bot, TRADE_PAIRS, SCAN_INTERVAL, EXCHANGE

app = Flask(__name__, static_folder="static")
CORS(app)

bot_thread = None


# ─── SERVE DASHBOARD ─────────────────────────────────────
@app.route("/")
def dashboard():
    return send_from_directory("static", "index.html")


# ─── BOT CONTROL ─────────────────────────────────────────
@app.route("/api/start", methods=["POST"])
def start_bot():
    global bot_thread
    data = request.json or {}
    bot.paper_mode = not data.get("live_mode", False)
    if not bot.running:
        bot_thread = threading.Thread(target=bot.run, daemon=True)
        bot_thread.start()
        return jsonify({"success": True, "mode": "LIVE" if not bot.paper_mode else "PAPER"})
    return jsonify({"success": False, "message": "Already running"})


@app.route("/api/stop", methods=["POST"])
def stop_bot():
    bot.running = False
    return jsonify({"success": True})


@app.route("/api/status")
def status():
    return jsonify(bot.get_status())


@app.route("/api/mode", methods=["POST"])
def set_mode():
    mode = (request.json or {}).get("mode", "paper")
    bot.paper_mode = (mode == "paper")
    return jsonify({"success": True, "mode": mode})


# ─── MARKET DATA ─────────────────────────────────────────
@app.route("/api/tickers")
def all_tickers():
    return jsonify(bot.client.get_all_tickers())


@app.route("/api/ticker/<path:symbol>")
def ticker(symbol):
    return jsonify(bot.client.get_ticker_24hr(symbol.replace("-", "/")))


@app.route("/api/portfolio")
def portfolio():
    return jsonify(bot.client.get_portfolio())


# ─── ORDERS ──────────────────────────────────────────────
@app.route("/api/orders/open")
def open_orders():
    return jsonify(bot.client.get_open_orders())


@app.route("/api/orders/cancel", methods=["POST"])
def cancel_order():
    oid = (request.json or {}).get("order_id")
    return jsonify(bot.client.cancel_order(oid))


# ─── POLYMARKET ───────────────────────────────────────────
@app.route("/api/polymarket/status")
def poly_status():
    if bot.arb_scanner:
        return jsonify(bot.arb_scanner.get_status())
    return jsonify({"error": "Polymarket engine not loaded"})


@app.route("/api/polymarket/mispricing")
def poly_mispricing():
    if bot.arb_scanner:
        return jsonify({"alerts": bot.arb_scanner.get_status().get("mispricing_alerts", [])})
    return jsonify({"alerts": []})


@app.route("/api/polymarket/arb")
def poly_arb():
    if bot.arb_scanner:
        return jsonify({"alerts": bot.arb_scanner.get_status().get("arb_alerts", [])})
    return jsonify({"alerts": []})


# ─── CONFIG / HEALTH ─────────────────────────────────────
@app.route("/api/config")
def config():
    return jsonify({
        "pairs": TRADE_PAIRS, "exchange": EXCHANGE,
        "scan_interval": SCAN_INTERVAL, "paper_mode": bot.paper_mode,
        "max_trade_amount": float(os.environ.get("MAX_TRADE_USDT", "50")),
        "keys_configured": bot.get_status().get("keys_configured", False)
    })


@app.route("/api/validate")
def validate():
    return jsonify(bot.client.validate_keys())


@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot_running": bot.running})


# ─── AUTO-START BOT ON LAUNCH ────────────────────────────
def auto_start():
    """Start bot automatically when server starts on Render"""
    live = os.environ.get("LIVE_TRADING", "false").lower() == "true"
    bot.paper_mode = not live
    if not bot.running:
        t = threading.Thread(target=bot.run, daemon=True)
        t.start()
        print(f"✅ Bot auto-started in {'LIVE' if live else 'PAPER'} mode")


if __name__ == "__main__":
    auto_start()
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 NEXUS TRADER running on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
else:
    # Called by gunicorn
    auto_start()
