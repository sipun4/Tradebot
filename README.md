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
In Render → Your Service → Environment, add these:

| Variable | Value | Required |
|----------|-------|----------|
| `COINSWITCH_API_KEY` | Your CoinSwitch API key | ✅ Yes |
| `COINSWITCH_SECRET_KEY` | Your CoinSwitch secret key | ✅ Yes |
| `GROQ_API_KEY` | Your Groq API key | ✅ Yes |
| `LIVE_TRADING` | `false` (paper) or `true` (real money) | Optional |
| `MAX_TRADE_USDT` | `50` | Optional |
| `SCAN_INTERVAL` | `10` | Optional |
| `COINSWITCH_EXCHANGE` | `c2c2` | Optional |

### Step 4 — Deploy
Click **Deploy** — Render will install dependencies and start the server.

### Step 5 — Access Dashboard
Your dashboard will be live at:
`https://your-service-name.onrender.com`

---

## 🔑 Getting API Keys

**Groq API Key (Free)**
- https://console.groq.com → API Keys → Create

**CoinSwitch API Key**
- https://coinswitch.co/pro/profile?section=api-trading
- Generate API Key + Secret Key

---

## ⚠️ Important
- `LIVE_TRADING=false` by default — uses paper trading (safe, no real money)
- Set `LIVE_TRADING=true` only when you're ready to trade real money
- Render free tier may sleep after inactivity — upgrade to paid for 24/7 uptime

---

## 📁 File Structure
```
nexus_trader/
├── server.py            # Flask server (entry point)
├── bot.py               # Trading engine
├── polymarket_engine.py # Mispricing + Arb scanner
├── static/
│   └── index.html       # Dashboard UI
├── requirements.txt
├── Procfile             # Render process file
├── render.yaml          # Render config
└── README.md
```
