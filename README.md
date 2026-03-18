# NEXUS TRADER — Render Deployment Guide

## 🚀 Deploy to Render in 5 Steps

### Step 1 — Upload to GitHub
Create a new GitHub repo and push all these files.

### Step 2 — Create Render Web Service
1. Go to https://render.com → New → Web Service
2. Connect your GitHub repo
3. Settings:
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`

### Step 3 — Add Environment Variables
In Render → Your Service → **Environment** tab:

| Variable | Description | Required |
|----------|-------------|----------|
| `COINSWITCH_API_KEY` | Your CoinSwitch API key | ✅ |
| `COINSWITCH_SECRET_KEY` | Your CoinSwitch secret key | ✅ |
| `GROQ_API_KEY` | Your Groq API key | ✅ |
| `DASHBOARD_PASSWORD` | Password to access the dashboard | ✅ |
| `FLASK_SECRET_KEY` | Any random string for session security | ✅ |
| `LIVE_TRADING` | `false` = paper trading, `true` = real money | Optional |
| `MAX_TRADE_USDT` | Max USDT per trade (default: 50) | Optional |
| `SCAN_INTERVAL` | Seconds between scans (default: 10) | Optional |
| `COINSWITCH_EXCHANGE` | Exchange to use (default: c2c2) | Optional |

### Step 4 — Deploy
Click **Deploy**.

### Step 5 — Access Dashboard
Visit `https://your-service-name.onrender.com`
Enter your `DASHBOARD_PASSWORD` to log in.

---

## 🔑 Getting API Keys

**Groq (Free):** https://console.groq.com → API Keys  
**CoinSwitch:** https://coinswitch.co/pro/profile?section=api-trading

---

## 📁 File Structure
```
nexus_trader/
├── server.py            # Flask server + auth
├── bot.py               # Trading engine
├── polymarket_engine.py # Mispricing + Arb scanner
├── static/
│   ├── index.html       # Dashboard (protected)
│   └── login.html       # Login page
├── requirements.txt
├── Procfile
├── render.yaml
└── README.md
```
