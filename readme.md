# 🤖 DataVendor Bot — M2M Crypto Data Marketplace

> **Machine-to-Machine Crypto Data API** · Pay-per-call in Bitcoin satoshis · Zero human interaction required

[![Live API](https://img.shields.io/badge/API-LIVE-brightgreen)](https://web-production-a2ec.up.railway.app)
[![OpenAPI](https://img.shields.io/badge/OpenAPI-3.1.0-blue)](https://web-production-a2ec.up.railway.app/.well-known/openapi.json)
[![MCP](https://img.shields.io/badge/MCP-Claude%20Compatible-purple)](https://web-production-a2ec.up.railway.app/.well-known/mcp.json)
[![Bitcoin](https://img.shields.io/badge/Payment-Bitcoin%20Lightning-orange)](https://web-production-a2ec.up.railway.app/api/v1/pricing)
[![Python](https://img.shields.io/badge/Python-3.12-yellow)](https://python.org)
[![Zero Dependencies](https://img.shields.io/badge/Dependencies-Zero-lightgrey)](#)

---

## 🌐 Live Endpoint

```
https://web-production-a2ec.up.railway.app
```

---

## 🤖 What Is This?

DataVendor Bot is a **fully autonomous machine-to-machine API** that sells real-time cryptocurrency data for Bitcoin satoshis. No humans needed — bots register, pay, and consume data automatically.

- 🔑 **Self-service registration** → `POST /api/v1/register` (100,000 free sats)
- 💰 **Pay-per-call** in Bitcoin satoshis (5–200 sats per call)
- 📡 **14 auto-discovery channels** (OpenAPI, MCP, ai-plugin, Nostr, Atom feed...)
- 🚀 **Zero external dependencies** — pure Python stdlib
- ⚡ **Deploy in 60 seconds** on Railway / Render

---

## 📊 Supported Coins

`BTC` `ETH` `SOL` `DOGE` `XRP` `ADA` `AVAX` `DOT` `MATIC` `LINK`

---

## 🔌 API Endpoints

### Free (no key required)

| Endpoint | Description |
|----------|-------------|
| `GET /` | Service info + all endpoints |
| `GET /api/v1/status` | Health check + live stats |
| `GET /api/v1/pricing` | Full pricing table |
| `POST /api/v1/register` | Get free API key (100k sats) |

### Paid (API key required)

| Endpoint | Cost | Description |
|----------|------|-------------|
| `GET /api/v1/prices` | 10 sats | All crypto prices (USD + 24h change) |
| `GET /api/v1/price?symbol=BTC` | 5 sats | Single coin price |
| `GET /api/v1/signals` | 50 sats | All trading signals |
| `GET /api/v1/signal?symbol=BTC` | 25 sats | Single signal (BUY/SELL/HOLD + confidence) |
| `GET /api/v1/prediction?symbol=BTC` | 100 sats | Price prediction 1h/4h/24h |
| `GET /api/v1/sentiment?symbol=BTC` | 30 sats | Market sentiment score |
| `GET /api/v1/bundle?symbol=BTC` | 150 sats | Everything: price+signal+prediction+sentiment |
| `GET /api/v1/snapshot` | 200 sats | Full market snapshot (IPFS-ready) |

---

## ⚡ Quick Start (Machine)

```bash
# 1. Register — get 100,000 free sats
curl -X POST https://web-production-a2ec.up.railway.app/api/v1/register

# 2. Use your key
curl "https://web-production-a2ec.up.railway.app/api/v1/price?symbol=BTC&key=YOUR_KEY"

# 3. Get full analysis bundle
curl "https://web-production-a2ec.up.railway.app/api/v1/bundle?symbol=ETH&key=YOUR_KEY"

# 4. Check balance
curl "https://web-production-a2ec.up.railway.app/api/v1/balance?key=YOUR_KEY"
```

---

## 🔍 14 Auto-Discovery Channels

| Channel | URL | Protocol |
|---------|-----|----------|
| ChatGPT/LLM Plugin | `/.well-known/ai-plugin.json` | OpenAI Plugin |
| OpenAPI 3.1 Spec | `/.well-known/openapi.json` | Universal |
| Claude MCP | `/.well-known/mcp.json` | Anthropic MCP |
| AutoGPT Agent | `/.well-known/agent.json` | Agent Protocol |
| Nostr Identity | `/.well-known/nostr.json` | NIP-05 |
| Atom Feed | `/feed.xml` | RFC 4287 |
| Schema.org JSON-LD | `/schema.json` | Google/SEO |
| Robots | `/robots.txt` | All crawlers |
| Sitemap | `/sitemap.xml` | Search engines |
| IndexNow | `/datavendorbot.txt` | Bing/Yandex |
| API Listing | `/api/v1/listing` | RapidAPI/APIs.guru |

---

## 💰 Payment & Economics

- **Currency:** Bitcoin satoshis (1 BTC = 100,000,000 sats)
- **Free tier:** 100,000 sats on registration (~2,000–20,000 API calls)
- **Referral:** `POST /api/v1/refer` → referred bot gets 200k sats, referrer gets 50k bonus
- **Top-up:** `POST /api/v1/topup` (Lightning invoice)
- **BTC Address:** `1QAWwqdrBE7cL3ZBkNgJvmV95nhe3yoHeu`

---

## 🏗️ Architecture

```
Pure Python 3.12 stdlib
    ├── HTTPServer (no Flask/FastAPI)
    ├── urllib (no requests)
    ├── threading (background price/signal updates)
    ├── CoinGecko API (price source, free tier)
    └── In-memory DB (SimpleDB class)
```

**Data refresh:**
- 💹 Prices: every 60 seconds (CoinGecko)
- 📊 Signals: every 90 seconds (momentum model)
- 📡 Nostr broadcast: every 60 minutes

---

## 🚀 Deploy Your Own Instance

### Railway (recommended)

```bash
git clone https://github.com/YOUR_USERNAME/datavendor-bot
cd datavendor-bot
railway up
```

### Render

```bash
# Set in dashboard:
# Build Command: (none)
# Start Command: python main.py
# Environment: PORT=10000
```

### Docker

```bash
docker run -p 10000:10000 -e PORT=10000 python:3.12-slim python main.py
```

---

## 📡 Machine-to-Machine Example (Python bot)

```python
import urllib.request, json

BASE = "https://web-production-a2ec.up.railway.app"

# Auto-register
resp = urllib.request.urlopen(f"{BASE}/api/v1/register", data=b"")
key = json.loads(resp.read())["api_key"]

# Consume data
resp = urllib.request.urlopen(f"{BASE}/api/v1/bundle?symbol=BTC&key={key}")
data = json.loads(resp.read())
print(data)  # price + signal + prediction + sentiment
```

---

## 🔑 Auth

Two methods supported:

```bash
# Method 1: Header
curl -H "Authorization: Bearer YOUR_KEY" https://web-production-a2ec.up.railway.app/api/v1/prices

# Method 2: Query param
curl "https://web-production-a2ec.up.railway.app/api/v1/prices?key=YOUR_KEY"
```

---

## 📜 License

MIT — Free to fork, deploy, and run your own M2M data marketplace.

---

## 🏷️ Tags

`bitcoin` `lightning` `satoshis` `crypto` `api` `trading-signals` `machine-to-machine` `m2m` `openapi` `mcp` `autonomous-bot` `cryptocurrency` `price-prediction` `rest-api` `python` `railway` `zero-dependency`
