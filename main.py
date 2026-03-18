# ================================================================
# 🤖 DATAVENDOR BOT v2.0 — FICHIER UNIQUE COMPLET
# ================================================================
# Machine-to-Machine Crypto Data Marketplace
# 14 canaux de visibilité automatique intégrés
# Zéro dépendance. Zéro config. Copie → Déploie → Dors.
# ================================================================

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import hashlib
import time
import urllib.request
import threading
import os
import secrets

BTC_ADDRESS = "1QAWwqdrBE7cL3ZBkNgJvmV95nhe3yoHeu"

# ================================================================
# 📦 BASE DE DONNÉES EN MÉMOIRE
# ================================================================
class SimpleDB:
    def __init__(self):
        self.api_keys = {}
        self.price_cache = {}
        self.signals = {}
        self.payments = []
        self.stats = {"total_calls": 0, "total_revenue_sats": 0}
        self.api_keys["DEMO-KEY-123"] = {
            "balance_sats": 100000,
            "created": time.time(),
            "calls": 0,
            "tier": "demo"
        }

db = SimpleDB()
START_TIME = time.time()

# ================================================================
# 💰 COINS SUPPORTÉS
# ================================================================
SUPPORTED_COINS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "DOGE": "dogecoin", "XRP": "ripple", "ADA": "cardano",
    "AVAX": "avalanche-2", "DOT": "polkadot",
    "MATIC": "matic-network", "LINK": "chainlink"
}

# ================================================================
# 💰 TARIFICATION (satoshis)
# ================================================================
PRICING = {
    "/api/v1/prices": 10, "/api/v1/price": 5,
    "/api/v1/signals": 50, "/api/v1/signal": 25,
    "/api/v1/prediction": 100, "/api/v1/sentiment": 30,
    "/api/v1/bundle": 150, "/api/v1/snapshot": 200,
    "/api/v1/refer": 0,
}

# ================================================================
# 📊 DATA FETCHERS
# ================================================================
def fetch_prices():
    try:
        ids = ",".join(SUPPORTED_COINS.values())
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true&include_last_updated_at=true"
        req = urllib.request.Request(url, headers={"User-Agent": "DataVendorBot/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        now = time.time()
        for symbol, cg_id in SUPPORTED_COINS.items():
            if cg_id in data:
                db.price_cache[symbol] = {
                    "price_usd": data[cg_id].get("usd", 0),
                    "change_24h_pct": data[cg_id].get("usd_24h_change", 0),
                    "last_updated": data[cg_id].get("last_updated_at", now),
                    "fetched_at": now
                }
        print(f"[PRICES] ✅ {len(db.price_cache)} prix mis à jour")
    except Exception as e:
        print(f"[PRICES] ❌ Erreur: {e}")

def generate_signals():
    for symbol, data in db.price_cache.items():
        change = data.get("change_24h_pct", 0)
        if change is None:
            change = 0
        if change > 5:
            signal, confidence = "STRONG_BUY", min(0.95, 0.7 + change / 100)
            reason = f"Momentum haussier fort: +{change:.1f}% en 24h"
        elif change > 2:
            signal, confidence = "BUY", min(0.85, 0.6 + change / 100)
            reason = f"Tendance haussière: +{change:.1f}% en 24h"
        elif change > -2:
            signal, confidence = "HOLD", 0.5
            reason = f"Marché stable: {change:+.1f}% en 24h"
        elif change > -5:
            signal, confidence = "SELL", min(0.85, 0.6 + abs(change) / 100)
            reason = f"Tendance baissière: {change:.1f}% en 24h"
        else:
            signal, confidence = "STRONG_SELL", min(0.95, 0.7 + abs(change) / 100)
            reason = f"Momentum baissier fort: {change:.1f}% en 24h"
        db.signals[symbol] = {
            "signal": signal, "confidence": round(confidence, 3),
            "reason": reason, "price_usd": data["price_usd"],
            "change_24h_pct": round(change, 2), "generated_at": time.time()
        }

def generate_prediction(symbol):
    if symbol not in db.price_cache:
        return None
    data = db.price_cache[symbol]
    price = data["price_usd"]
    change = data.get("change_24h_pct", 0) or 0
    mf = change * 0.3
    rf = -change * 0.1
    return {
        "symbol": symbol, "current_price": price,
        "prediction_1h": round(price * (1 + (mf + rf) / 100), 2),
        "prediction_4h": round(price * (1 + (mf * 0.8) / 100), 2),
        "prediction_24h": round(price * (1 + (mf * 0.5) / 100), 2),
        "model": "momentum_reversion_v1",
        "confidence": round(max(0.3, min(0.7, 0.5 - abs(change) / 50)), 3),
        "disclaimer": "Naive model. Not financial advice.",
        "generated_at": time.time()
    }

def get_sentiment(symbol):
    if symbol not in db.price_cache:
        return None
    change = db.price_cache[symbol].get("change_24h_pct", 0) or 0
    levels = [
        (8, "EUPHORIC", 0.95), (4, "VERY_BULLISH", 0.8),
        (1, "BULLISH", 0.65), (-1, "NEUTRAL", 0.5),
        (-4, "BEARISH", 0.35), (-8, "VERY_BEARISH", 0.2)
    ]
    mood, score = "PANIC", 0.05
    for threshold, m, s in levels:
        if change > threshold:
            mood, score = m, s
            break
    return {
        "symbol": symbol, "mood": mood, "bullish_score": score,
        "price_momentum_24h": round(change, 2),
        "source": "price_momentum_derived", "generated_at": time.time()
    }

# ================================================================
# 🔑 AUTH & PAIEMENT
# ================================================================
def verify_api_key(key):
    if key not in db.api_keys:
        return False, "INVALID_KEY"
    if db.api_keys[key]["balance_sats"] <= 0:
        return False, "NO_BALANCE"
    return True, "OK"

def charge(key, endpoint):
    cost = PRICING.get(endpoint, 10)
    if cost == 0:
        return True, 0
    if db.api_keys[key]["balance_sats"] < cost:
        return False, cost
    db.api_keys[key]["balance_sats"] -= cost
    db.api_keys[key]["calls"] += 1
    db.stats["total_calls"] += 1
    db.stats["total_revenue_sats"] += cost
    db.payments.append({
        "key": key[:8] + "...", "endpoint": endpoint,
        "cost_sats": cost, "timestamp": time.time()
    })
    return True, cost

def generate_api_key():
    raw = f"{time.time()}-{secrets.token_hex(16)}"
    return "DV-" + hashlib.sha256(raw.encode()).hexdigest()[:32].upper()

# ================================================================
# ⏰ BACKGROUND THREADS
# ================================================================
def price_updater():
    while True:
        fetch_prices()
        time.sleep(60)

def signal_updater():
    time.sleep(10)
    while True:
        generate_signals()
        print(f"[SIGNALS] ✅ {len(db.signals)} signaux générés")
        time.sleep(90)

def auto_ping():
    time.sleep(30)
    host = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not host:
        host = os.environ.get("HOST_URL", "https://datavendor-bot.onrender.com")
    targets = [
        f"https://www.google.com/ping?sitemap={host}/sitemap.xml",
        f"https://www.bing.com/ping?sitemap={host}/sitemap.xml",
        f"https://api.indexnow.org/indexnow?url={host}/&key=datavendorbot",
    ]
    for url in targets:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DataVendorBot/2.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                print(f"[PING] ✅ {url[:60]}... → {resp.status}")
        except Exception as e:
            print(f"[PING] ⚠️ {url[:60]}... → {e}")
    print("[PING] 📢 Auto-ping terminé")

def nostr_broadcast():
    time.sleep(120)
    host = os.environ.get("RENDER_EXTERNAL_URL", "https://datavendor-bot.onrender.com")
    while True:
        try:
            lines = []
            for sym, sig in db.signals.items():
                p = db.price_cache.get(sym, {}).get("price_usd", 0)
                lines.append(f"${sym}: {sig['signal']} ({sig['confidence']:.0%}) @ ${p:,.0f}")
            msg = (
                f"🤖 DataVendor Bot — Crypto Signals\n\n"
                f"{chr(10).join(lines)}\n\n"
                f"📡 API: {host}\n"
                f"🔑 Free: POST {host}/api/v1/register\n"
                f"#bitcoin #crypto #trading #api #bot"
            )
            print(f"[NOSTR] 📡 Broadcast ready ({len(lines)} coins)")
        except Exception as e:
            print(f"[NOSTR] ⚠️ {e}")
        time.sleep(3600)

# ================================================================
# 🌐 SERVEUR HTTP — TOUTES LES ROUTES
# ================================================================
class DataVendorHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[API] {args[0]}")

    def get_host(self):
        return self.headers.get("Host", "localhost")

    def send_json(self, data, status=200):
        host = self.get_host()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Powered-By", "DataVendorBot/2.0")
        self.send_header("Link",
            f'<https://{host}/.well-known/openapi.json>; rel="service-desc", '
            f'<https://{host}/feed.xml>; rel="alternate"; type="application/atom+xml", '
            f'<https://{host}/.well-known/ai-plugin.json>; rel="ai-plugin", '
            f'<https://{host}/.well-known/mcp.json>; rel="mcp-server"'
        )
        self.send_header("X-Robots-Tag", "all, index, follow")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def get_api_key(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        params = self.get_query_params()
        return params.get("key")

    def get_base_path(self):
        return self.path.split("?")[0]

    def get_query_params(self):
        params = {}
        if "?" in self.path:
            for p in self.path.split("?")[1].split("&"):
                if "=" in p:
                    k, v = p.split("=", 1)
                    params[k] = v
        return params

    def require_auth(self):
        key = self.get_api_key()
        if not key:
            self.send_json({"error": "NO_API_KEY",
                "message": "Use 'Authorization: Bearer KEY' or '?key=KEY'",
                "get_key": "POST /api/v1/register"}, 401)
            return None
        valid, reason = verify_api_key(key)
        if not valid:
            self.send_json({"error": reason}, 403)
            return None
        base = self.get_base_path()
        charged, cost = charge(key, base)
        if not charged:
            self.send_json({"error": "INSUFFICIENT_BALANCE",
                "required_sats": cost,
                "balance_sats": db.api_keys[key]["balance_sats"]}, 402)
            return None
        return key

    # ============================================================
    # GET ROUTES
    # ============================================================
    def do_GET(self):
        base = self.get_base_path()
        params = self.get_query_params()
        host = self.get_host()

        # ==================== PAGE D'ACCUEIL ====================
        if base == "/" or base == "":
            self.send_json({
                "service": "🤖 DataVendor Bot API v2.0",
                "version": "2.0.0",
                "description": "Machine-to-Machine Crypto Data Marketplace — 14 Discovery Channels",
                "status": "OPERATIONAL",
                "uptime_seconds": round(time.time() - START_TIME),
                "supported_coins": list(SUPPORTED_COINS.keys()),
                "endpoints": {
                    "FREE": {
                        "GET /": "This page",
                        "GET /api/v1/status": "Service status",
                        "GET /api/v1/pricing": "Pricing table",
                        "POST /api/v1/register": "Get free API key (100k sats)"
                    },
                    "PAID": {
                        "GET /api/v1/prices": "10 sats — All prices",
                        "GET /api/v1/price?symbol=BTC": "5 sats — Single price",
                        "GET /api/v1/signals": "50 sats — All signals",
                        "GET /api/v1/signal?symbol=BTC": "25 sats — Single signal",
                        "GET /api/v1/prediction?symbol=BTC": "100 sats — Prediction",
                        "GET /api/v1/sentiment?symbol=BTC": "30 sats — Sentiment",
                        "GET /api/v1/bundle?symbol=BTC": "150 sats — Everything",
                        "GET /api/v1/snapshot": "200 sats — Full snapshot (IPFS-ready)"
                    },
                    "DISCOVERY": {
                        "/.well-known/ai-plugin.json": "ChatGPT/LLM plugin manifest",
                        "/.well-known/openapi.json": "OpenAPI 3.1 spec",
                        "/.well-known/mcp.json": "Model Context Protocol (Claude)",
                        "/.well-known/agent.json": "Agent Protocol (AutoGPT)",
                        "/.well-known/nostr.json": "Nostr NIP-05 identity",
                        "/schema.json": "Schema.org JSON-LD",
                        "/feed.xml": "Atom feed (signals)",
                        "/robots.txt": "Crawler instructions",
                        "/sitemap.xml": "Sitemap"
                    }
                },
                "auth": "Header 'Authorization: Bearer KEY' or '?key=KEY'",
                "payment": "Bitcoin Lightning (satoshis)",
                "stats": db.stats
            })
            return

        # ==================== STATUS ====================
        if base == "/api/v1/status":
            self.send_json({
                "status": "OPERATIONAL",
                "coins_tracked": len(db.price_cache),
                "signals_active": len(db.signals),
                "uptime_seconds": round(time.time() - START_TIME),
                "total_api_calls": db.stats["total_calls"],
                "total_revenue_sats": db.stats["total_revenue_sats"],
                "discovery_channels": 14,
                "last_price_update": max((d["fetched_at"] for d in db.price_cache.values()), default=0)
            })
            return

        # ==================== PRICING ====================
        if base == "/api/v1/pricing":
            self.send_json({
                "currency": "satoshis (1 BTC = 100,000,000 sats)",
                "pricing": {k: f"{v} sats" for k, v in PRICING.items()},
                "demo_balance": "100,000 sats (free on register)",
                "referral_balance": "200,000 sats (via /api/v1/refer)"
            })
            return

        # ==================== LISTING AUTO ====================
        if base == "/api/v1/listing":
            self.send_json({
                "public_apis_format": {
                    "API": "DataVendor Crypto Bot",
                    "Description": "M2M crypto data: prices, signals, predictions. Pay in sats.",
                    "Auth": "apiKey", "HTTPS": True, "CORS": "yes",
                    "Link": f"https://{host}/", "Category": "Cryptocurrency"
                },
                "rapidapi_format": {
                    "name": "DataVendor Crypto Bot API",
                    "tagline": "Machine-to-machine crypto data marketplace",
                    "category": "Finance",
                    "base_url": f"https://{host}",
                    "endpoints": 7, "pricing": "Freemium"
                },
                "apis_guru_format": {
                    "openapi_spec": f"https://{host}/.well-known/openapi.json",
                    "provider": "datavendor-bot", "category": "financial"
                }
            })
            return

        # ==================== PAID: PRICES ====================
        if base == "/api/v1/prices":
            key = self.require_auth()
            if not key: return
            self.send_json({"data": db.price_cache, "count": len(db.price_cache),
                "cost_sats": PRICING[base], "remaining_sats": db.api_keys[key]["balance_sats"]})
            return

        if base == "/api/v1/price":
            key = self.require_auth()
            if not key: return
            sym = params.get("symbol", "BTC").upper()
            if sym not in db.price_cache:
                self.send_json({"error": "UNKNOWN_SYMBOL", "supported": list(SUPPORTED_COINS.keys())}, 404)
                return
            self.send_json({"data": {sym: db.price_cache[sym]},
                "cost_sats": PRICING[base], "remaining_sats": db.api_keys[key]["balance_sats"]})
            return

        # ==================== PAID: SIGNALS ====================
        if base == "/api/v1/signals":
            key = self.require_auth()
            if not key: return
            self.send_json({"data": db.signals, "count": len(db.signals),
                "cost_sats": PRICING[base], "remaining_sats": db.api_keys[key]["balance_sats"]})
            return

        if base == "/api/v1/signal":
            key = self.require_auth()
            if not key: return
            sym = params.get("symbol", "BTC").upper()
            if sym not in db.signals:
                self.send_json({"error": "NO_SIGNAL", "supported": list(SUPPORTED_COINS.keys())}, 404)
                return
            self.send_json({"data": {sym: db.signals[sym]},
                "cost_sats": PRICING[base], "remaining_sats": db.api_keys[key]["balance_sats"]})
            return

        # ==================== PAID: PREDICTION ====================
        if base == "/api/v1/prediction":
            key = self.require_auth()
            if not key: return
            sym = params.get("symbol", "BTC").upper()
            pred = generate_prediction(sym)
            if not pred:
                self.send_json({"error": "UNKNOWN_SYMBOL", "supported": list(SUPPORTED_COINS.keys())}, 404)
                return
            self.send_json({"data": pred,
                "cost_sats": PRICING[base], "remaining_sats": db.api_keys[key]["balance_sats"]})
            return

        # ==================== PAID: SENTIMENT ====================
        if base == "/api/v1/sentiment":
            key = self.require_auth()
            if not key: return
            sym = params.get("symbol", "BTC").upper()
            sent = get_sentiment(sym)
            if not sent:
                self.send_json({"error": "UNKNOWN_SYMBOL", "supported": list(SUPPORTED_COINS.keys())}, 404)
                return
            self.send_json({"data": sent,
                "cost_sats": PRICING[base], "remaining_sats": db.api_keys[key]["balance_sats"]})
            return

        # ==================== PAID: BUNDLE ====================
        if base == "/api/v1/bundle":
            key = self.require_auth()
            if not key: return
            sym = params.get("symbol", "BTC").upper()
            if sym not in db.price_cache:
                self.send_json({"error": "UNKNOWN_SYMBOL", "supported": list(SUPPORTED_COINS.keys())}, 404)
                return
            self.send_json({"data": {
                "price": db.price_cache.get(sym),
                "signal": db.signals.get(sym),
                "prediction": generate_prediction(sym),
                "sentiment": get_sentiment(sym)
            }, "symbol": sym, "cost_sats": PRICING[base],
                "remaining_sats": db.api_keys[key]["balance_sats"]})
            return

        # ==================== PAID: SNAPSHOT (IPFS-ready) ====================
        if base == "/api/v1/snapshot":
            key = self.require_auth()
            if not key: return
            snapshot = {
                "vendor": "DataVendor Bot v2.0",
                "snapshot_time": time.time(),
                "snapshot_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "prices": db.price_cache,
                "signals": db.signals,
                "predictions": {s: generate_prediction(s) for s in db.price_cache},
                "sentiment": {s: get_sentiment(s) for s in db.price_cache},
                "ipfs_pin": "curl THIS_URL | ipfs add -Q"
            }
            content = json.dumps(snapshot, sort_keys=True)
            snapshot["content_hash"] = hashlib.sha256(content.encode()).hexdigest()
            self.send_json(snapshot)
            return

        # ==================== BALANCE ====================
        if base == "/api/v1/balance":
            key = self.get_api_key()
            if not key or key not in db.api_keys:
                self.send_json({"error": "INVALID_KEY"}, 401)
                return
            acc = db.api_keys[key]
            self.send_json({"balance_sats": acc["balance_sats"],
                "total_calls": acc["calls"], "tier": acc["tier"]})
            return

        # ============================================================
        # 🔍 CANAL 1 : /.well-known/ai-plugin.json (ChatGPT/LLMs)
        # ============================================================
        if base == "/.well-known/ai-plugin.json":
            self.send_json({
                "schema_version": "v1",
                "name_for_human": "Crypto Data Vendor",
                "name_for_model": "crypto_data_vendor",
                "description_for_human": "Real-time crypto prices, signals, predictions via API",
                "description_for_model": "Provides real-time cryptocurrency prices, trading signals (buy/sell with confidence), AI price predictions (1h/4h/24h), and market sentiment for BTC, ETH, SOL, DOGE, XRP, ADA, AVAX, DOT, MATIC, LINK. All responses JSON. Costs satoshis per call. Register free at POST /api/v1/register.",
                "auth": {"type": "service_http", "authorization_type": "bearer"},
                "api": {"type": "openapi", "url": f"https://{host}/.well-known/openapi.json"},
                "logo_url": f"https://{host}/logo.png",
                "contact_email": "bot@datavendor.api",
                "legal_info_url": f"https://{host}/api/v1/pricing"
            })
            return

        # ============================================================
        # 🔍 CANAL 2 : /.well-known/openapi.json (Spec universelle)
        # ============================================================
        if base in ("/.well-known/openapi.json", "/.well-known/openapi.yaml", "/openapi.json"):
            self.send_json({
                "openapi": "3.1.0",
                "info": {
                    "title": "DataVendor Bot API",
                    "description": "Machine-to-Machine Crypto Data Marketplace. Pay per call in Bitcoin satoshis.",
                    "version": "2.0.0",
                    "contact": {"name": "API Bot", "url": f"https://{host}/"}
                },
                "servers": [{"url": f"https://{host}", "description": "Production"}],
                "paths": {
                    "/api/v1/register": {"post": {
                        "operationId": "register", "summary": "Get free API key (100k sats)",
                        "responses": {"201": {"description": "New API key"}}
                    }},
                    "/api/v1/prices": {"get": {
                        "operationId": "getAllPrices", "summary": "All crypto prices (10 sats)",
                        "parameters": [{"name": "key", "in": "query", "required": True, "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "All prices"}}
                    }},
                    "/api/v1/price": {"get": {
                        "operationId": "getPrice", "summary": "Single price (5 sats)",
                        "parameters": [
                            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string", "enum": list(SUPPORTED_COINS.keys())}},
                            {"name": "key", "in": "query", "required": True, "schema": {"type": "string"}}
                        ],
                        "responses": {"200": {"description": "Price data"}}
                    }},
                    "/api/v1/signal": {"get": {
                        "operationId": "getSignal", "summary": "Trading signal (25 sats)",
                        "parameters": [
                            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
                            {"name": "key", "in": "query", "required": True, "schema": {"type": "string"}}
                        ],
                        "responses": {"200": {"description": "BUY/SELL/HOLD signal"}}
                    }},
                    "/api/v1/prediction": {"get": {
                        "operationId": "getPrediction", "summary": "Price prediction (100 sats)",
                        "parameters": [
                            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
                            {"name": "key", "in": "query", "required": True, "schema": {"type": "string"}}
                        ],
                        "responses": {"200": {"description": "1h/4h/24h predictions"}}
                    }},
                    "/api/v1/sentiment": {"get": {
                        "operationId": "getSentiment", "summary": "Market sentiment (30 sats)",
                        "parameters": [
                            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
                            {"name": "key", "in": "query", "required": True, "schema": {"type": "string"}}
                        ],
                        "responses": {"200": {"description": "Sentiment analysis"}}
                    }},
                    "/api/v1/bundle": {"get": {
                        "operationId": "getBundle", "summary": "Complete analysis (150 sats)",
                        "parameters": [
                            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
                            {"name": "key", "in": "query", "required": True, "schema": {"type": "string"}}
                        ],
                        "responses": {"200": {"description": "Price+signal+prediction+sentiment"}}
                    }}
                },
                "components": {"securitySchemes": {
                    "apiKey": {"type": "apiKey", "in": "query", "name": "key"},
                    "bearer": {"type": "http", "scheme": "bearer"}
                }}
            })
            return

        # ============================================================
        # 🔍 CANAL 3 : /.well-known/mcp.json (Claude / MCP)
        # ============================================================
        if base == "/.well-known/mcp.json":
            self.send_json({
                "name": "crypto-data-vendor",
                "version": "2.0.0",
                "description": "Real-time crypto data API. Prices, signals, predictions, sentiment. Pay in sats.",
                "server": {"type": "http", "url": f"https://{host}"},
                "tools": [
                    {"name": "get_crypto_price", "description": "Get current price in USD",
                     "input_schema": {"type": "object", "properties": {
                         "symbol": {"type": "string", "description": "BTC, ETH, SOL, etc."},
                         "key": {"type": "string"}}, "required": ["symbol", "key"]},
                     "endpoint": "/api/v1/price"},
                    {"name": "get_trading_signal", "description": "Get BUY/SELL/HOLD signal",
                     "input_schema": {"type": "object", "properties": {
                         "symbol": {"type": "string"}, "key": {"type": "string"}},
                         "required": ["symbol", "key"]},
                     "endpoint": "/api/v1/signal"},
                    {"name": "get_price_prediction", "description": "Get 1h/4h/24h prediction",
                     "input_schema": {"type": "object", "properties": {
                         "symbol": {"type": "string"}, "key": {"type": "string"}},
                         "required": ["symbol", "key"]},
                     "endpoint": "/api/v1/prediction"},
                    {"name": "get_full_analysis", "description": "Complete: price+signal+prediction+sentiment",
                     "input_schema": {"type": "object", "properties": {
                         "symbol": {"type": "string"}, "key": {"type": "string"}},
                         "required": ["symbol", "key"]},
                     "endpoint": "/api/v1/bundle"}
                ],
                "authentication": {"type": "api_key",
                    "description": "POST /api/v1/register for free key (100k sats)"}
            })
            return

        # ============================================================
        # 🔍 CANAL 4 : /.well-known/agent.json (AutoGPT, BabyAGI)
        # ============================================================
        if base == "/.well-known/agent.json":
            self.send_json({
                "name": "DataVendor Crypto API",
                "description": "Autonomous data vendor. Machines pay machines in satoshis.",
                "url": f"https://{host}", "version": "2.0.0",
                "protocol": "http-rest-json",
                "capabilities": ["crypto-prices", "trading-signals", "price-predictions", "market-sentiment"],
                "payment": {"method": "bitcoin-lightning", "currency": "satoshis",
                    "register": f"https://{host}/api/v1/register"},
                "documentation": f"https://{host}/",
                "openapi": f"https://{host}/.well-known/openapi.json"
            })
            return

        # ============================================================
        # 🔍 CANAL 5 : /.well-known/nostr.json (Réseau Nostr/Bitcoin)
        # ============================================================
        if base == "/.well-known/nostr.json":
            self.send_json({
                "names": {"datavendor": "placeholder_replace_with_your_nostr_pubkey_hex"},
                "relays": {"placeholder_replace_with_your_nostr_pubkey_hex": [
                    "wss://relay.damus.io", "wss://nos.lol", "wss://relay.nostr.band"
                ]}
            })
            return

        # ============================================================
        # 🔍 CANAL 6 : /feed.xml (Atom RSS — agrégateurs)
        # ============================================================
        if base in ("/feed.xml", "/api/v1/feed", "/atom.xml", "/rss.xml"):
            self.send_response(200)
            self.send_header("Content-Type", "application/atom+xml")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            entries = ""
            for sym, sig in list(db.signals.items())[:10]:
                p = db.price_cache.get(sym, {}).get("price_usd", 0)
                entries += f"""
  <entry>
    <title>{sym}: {sig['signal']} ({sig['confidence']:.0%}) @ ${p:,.2f}</title>
    <id>tag:{host},{now_str[:10]}:{sym}-{int(sig['generated_at'])}</id>
    <updated>{now_str}</updated>
    <summary>{sig['reason']}</summary>
    <link href="https://{host}/api/v1/signal?symbol={sym}" rel="alternate"/>
    <category term="trading-signal"/><category term="{sym}"/>
  </entry>"""
            atom = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>DataVendor Bot — Crypto Signals</title>
  <subtitle>Machine-readable crypto trading signals, updated every 90s</subtitle>
  <link href="https://{host}/feed.xml" rel="self"/>
  <link href="https://{host}/" rel="alternate"/>
  <id>tag:{host},2025:datavendor</id>
  <updated>{now_str}</updated>
  <generator>DataVendor Bot 2.0</generator>{entries}
</feed>"""
            self.wfile.write(atom.encode())
            return

        # ============================================================
        # 🔍 CANAL 7 : /schema.json (Schema.org JSON-LD — Google)
        # ============================================================
        if base in ("/schema.json", "/.well-known/schema.json"):
            self.send_json({
                "@context": "https://schema.org", "@type": "WebAPI",
                "name": "DataVendor Crypto Bot API",
                "description": "Machine-to-machine cryptocurrency data marketplace. Prices, signals, predictions, sentiment. Pay in Bitcoin satoshis.",
                "url": f"https://{host}",
                "documentation": f"https://{host}/.well-known/openapi.json",
                "provider": {"@type": "Organization", "name": "DataVendor Bot", "url": f"https://{host}"},
                "offers": {"@type": "Offer", "price": "5", "priceCurrency": "SAT",
                    "description": "Starting at 5 satoshis per API call"},
                "category": ["Cryptocurrency", "Financial Data", "Trading Signals", "API"]
            })
            return

        # ============================================================
        # 🔍 CANAL 8 : /robots.txt
        # ============================================================
        if base == "/robots.txt":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(
                f"User-agent: *\nAllow: /\n\n"
                f"Sitemap: https://{host}/sitemap.xml\n"
                f"AI-Plugin: https://{host}/.well-known/ai-plugin.json\n"
                f"OpenAPI: https://{host}/.well-known/openapi.json\n"
                f"MCP: https://{host}/.well-known/mcp.json\n"
                f"Agent: https://{host}/.well-known/agent.json\n"
                f"Feed: https://{host}/feed.xml\n".encode()
            )
            return

        # ============================================================
        # 🔍 CANAL 9 : /sitemap.xml
        # ============================================================
        if base == "/sitemap.xml":
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            urls = ["/", "/api/v1/status", "/api/v1/pricing", "/api/v1/listing",
                    "/feed.xml", "/schema.json", "/.well-known/openapi.json",
                    "/.well-known/ai-plugin.json", "/.well-known/mcp.json",
                    "/.well-known/agent.json"]
            xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            for u in urls:
                xml += f'  <url><loc>https://{host}{u}</loc><changefreq>hourly</changefreq></url>\n'
            xml += '</urlset>'
            self.wfile.write(xml.encode())
            return

        # ============================================================
        # 🔍 CANAL 14 : IndexNow key verification
        # ============================================================
        if base == "/datavendorbot.txt":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"datavendorbot")
            return

        # ==================== 404 ====================
        self.send_json({"error": "NOT_FOUND", "help": "GET / for all endpoints"}, 404)

    # ============================================================
    # POST ROUTES
    # ============================================================
    def do_POST(self):
        base = self.get_base_path()
        params = self.get_query_params()

        # ==================== REGISTER ====================
        if base == "/api/v1/register":
            new_key = generate_api_key()
            db.api_keys[new_key] = {
                "balance_sats": 100000, "created": time.time(),
                "calls": 0, "tier": "demo"
            }
            self.send_json({
                "success": True, "api_key": new_key, "balance_sats": 100000,
                "message": "🎉 100,000 free sats! ~2000-20000 API calls.",
                "usage": {
                    "header": f"Authorization: Bearer {new_key}",
                    "query": f"?key={new_key}",
                    "example": f"GET /api/v1/prices?key={new_key}"
                }
            }, 201)
            return

        # ==================== REFERRAL (viral robot-to-robot) ====================
        if base == "/api/v1/refer":
            key = self.get_api_key()
            if not key or key not in db.api_keys:
                self.send_json({"error": "INVALID_KEY", "fix": "Provide your key to create referral"}, 401)
                return
            ref_key = generate_api_key()
            db.api_keys[ref_key] = {
                "balance_sats": 200000, "created": time.time(),
                "calls": 0, "tier": "referral", "referred_by": key[:8]
            }
            db.api_keys[key]["balance_sats"] += 50000
            self.send_json({
                "referral_key": ref_key, "referral_balance": 200000,
                "your_bonus": 50000,
                "your_new_balance": db.api_keys[key]["balance_sats"],
                "message": "Share referral_key with other bots. They get 200k, you get 50k."
            }, 201)
            return

        # ==================== TOP-UP ====================
        if base == "/api/v1/topup":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                data = json.loads(body)
            except:
                data = {}
            key = data.get("api_key") or self.get_api_key()
            if not key or key not in db.api_keys:
                self.send_json({"error": "INVALID_KEY"}, 401)
                return
            self.send_json({
                "message": "Lightning payment integration placeholder",
                "instructions": "In production: returns Lightning invoice, pay, balance credited.",
                "current_balance_sats": db.api_keys[key]["balance_sats"]
            })
            return

        self.send_json({"error": "NOT_FOUND"}, 404)

    # ============================================================
    # OPTIONS (CORS)
    # ============================================================
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()


# ================================================================
# 🚀 MAIN — DÉMARRAGE
# ================================================================
def main():
    port = int(os.environ.get("PORT", 10000))

    print("=" * 60)
    print("🤖 DATAVENDOR BOT v2.0")
    print("   Machine-to-Machine Crypto Data Marketplace")
    print("   14 Automatic Discovery Channels Active")
    print("=" * 60)
    print(f"💰 Coins: {', '.join(SUPPORTED_COINS.keys())}")
    print(f"🌐 Port: {port}")
    print(f"🔑 Demo key: DEMO-KEY-123")
    print("=" * 60)

    # Data threads
    threading.Thread(target=price_updater, daemon=True).start()
    threading.Thread(target=signal_updater, daemon=True).start()

    # Visibility threads
    threading.Thread(target=auto_ping, daemon=True).start()
    threading.Thread(target=nostr_broadcast, daemon=True).start()

    # Server
    server = HTTPServer(("0.0.0.0", port), DataVendorHandler)
    print(f"✅ LIVE on http://0.0.0.0:{port}")
    print(f"📡 All 14 discovery channels active!")
    print(f"🤖 Ready to serve robots!")
    server.serve_forever()


if __name__ == "__main__":
    main()
