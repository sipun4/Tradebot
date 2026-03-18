"""
NEXUS TRADER - Flask Server (Render-ready)
- Password protection via DASHBOARD_PASSWORD env var
- Session-based auth with secure cookie
- All config via environment variables
"""

import os, threading, secrets
from flask import Flask, jsonify, request, send_from_directory, session, redirect, url_for
from flask_cors import CORS
from bot import bot, TRADE_PAIRS, SCAN_INTERVAL, EXCHANGE

app = Flask(__name__, static_folder="static")
CORS(app)

# Secret key for sessions — auto-generated or set via env
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

# Dashboard password from env var
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

bot_thread = None


# ═══════════════════════════════════════════════════════════
# AUTH HELPERS
# ═══════════════════════════════════════════════════════════

def is_authenticated():
    """Check if current session is authenticated"""
    if not DASHBOARD_PASSWORD:
        return True  # No password set = open access
    return session.get("authenticated") is True

def require_auth(f):
    """Decorator to protect routes"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_authenticated():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized", "code": 401}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════════════════════

@app.route("/login", methods=["GET"])
def login_page():
    if is_authenticated():
        return redirect("/")
    return send_from_directory("static", "login.html")


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.json or {}
    password = data.get("password", "")

    if not DASHBOARD_PASSWORD:
        return jsonify({"success": True, "message": "No password required"})

    if password == DASHBOARD_PASSWORD:
        session["authenticated"] = True
        session.permanent = True
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "message": "Invalid password"}), 401


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/auth/check")
def auth_check():
    return jsonify({"authenticated": is_authenticated()})


# ═══════════════════════════════════════════════════════════
# PROTECTED ROUTES
# ═══════════════════════════════════════════════════════════

@app.route("/")
@require_auth
def dashboard():
    return send_from_directory("static", "index.html")


@app.route("/api/start", methods=["POST"])
@require_auth
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
@require_auth
def stop_bot():
    bot.running = False
    return jsonify({"success": True})


@app.route("/api/status")
@require_auth
def status():
    return jsonify(bot.get_status())


@app.route("/api/mode", methods=["POST"])
@require_auth
def set_mode():
    mode = (request.json or {}).get("mode", "paper")
    bot.paper_mode = (mode == "paper")
    return jsonify({"success": True, "mode": mode})


@app.route("/api/tickers")
@require_auth
def all_tickers():
    return jsonify(bot.client.get_all_tickers())


@app.route("/api/ticker/<path:symbol>")
@require_auth
def ticker(symbol):
    return jsonify(bot.client.get_ticker_24hr(symbol.replace("-", "/")))


@app.route("/api/portfolio")
@require_auth
def portfolio():
    return jsonify(bot.client.get_portfolio())


@app.route("/api/orders/open")
@require_auth
def open_orders():
    return jsonify(bot.client.get_open_orders())


@app.route("/api/orders/cancel", methods=["POST"])
@require_auth
def cancel_order():
    oid = (request.json or {}).get("order_id")
    return jsonify(bot.client.cancel_order(oid))


@app.route("/api/polymarket/status")
@require_auth
def poly_status():
    if bot.arb_scanner:
        return jsonify(bot.arb_scanner.get_status())
    return jsonify({"error": "Polymarket engine not loaded"})


@app.route("/api/polymarket/mispricing")
@require_auth
def poly_mispricing():
    if bot.arb_scanner:
        return jsonify({"alerts": bot.arb_scanner.get_status().get("mispricing_alerts", [])})
    return jsonify({"alerts": []})


@app.route("/api/polymarket/arb")
@require_auth
def poly_arb():
    if bot.arb_scanner:
        return jsonify({"alerts": bot.arb_scanner.get_status().get("arb_alerts", [])})
    return jsonify({"alerts": []})


@app.route("/api/config")
@require_auth
def config():
    return jsonify({
        "pairs": TRADE_PAIRS, "exchange": EXCHANGE,
        "scan_interval": SCAN_INTERVAL, "paper_mode": bot.paper_mode,
        "max_trade_amount": float(os.environ.get("MAX_TRADE_USDT", "50")),
        "keys_configured": bot.get_status().get("keys_configured", False)
    })


@app.route("/api/validate")
@require_auth
def validate():
    return jsonify(bot.client.validate_keys())


# Health check — public (for Render uptime checks)
@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot_running": bot.running})


# ═══════════════════════════════════════════════════════════
# AUTO-START
# ═══════════════════════════════════════════════════════════

def auto_start():
    live = os.environ.get("LIVE_TRADING", "false").lower() == "true"
    bot.paper_mode = not live
    if not bot.running:
        t = threading.Thread(target=bot.run, daemon=True)
        t.start()
        print(f"✅ Bot auto-started in {'LIVE' if live else 'PAPER'} mode")
    if DASHBOARD_PASSWORD:
        print("🔒 Dashboard password protection ENABLED")
    else:
        print("⚠️  No DASHBOARD_PASSWORD set — dashboard is open to anyone")


if __name__ == "__main__":
    auto_start()
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 NEXUS TRADER on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
else:
    auto_start()
