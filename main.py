# ================================================================
# 🤖 DATAVENDOR BOT v3.0 — PAIX ETERNELLE EDITION
# ================================================================
# Machine-to-Machine Crypto Data Marketplace
# 
# UPGRADES v3.0 vs v2.0 :
#   ✅ UPGRADE 1 : SQLite persistant (Railway volume /data)
#                  → Les clés API survivent aux restarts
#   ✅ UPGRADE 2 : ThreadingMixIn (stdlib pure, zéro dépendance)
#                  → Des centaines de requêtes simultanées
#   ✅ UPGRADE 3 : OpenNode Lightning réel
#                  → Vrais paiements BTC, vrais revenus
#
# DEPLOY :
#   1. Mettre OPENNODE_API_KEY dans Railway environment variables
#   2. Monter un volume Railway sur /data (gratuit jusqu'à 1GB)
#   3. git push → c'est tout
# ================================================================

from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import json
import hashlib
import time
import urllib.request
import urllib.parse
import threading
import sqlite3
import os
import secrets

# ================================================================
# 🔧 CONFIGURATION
# ================================================================
BTC_ADDRESS    = "1QAWwqdrBE7cL3ZBkNgJvmV95nhe3yoHeu"
DB_PATH        = os.environ.get("DB_PATH", "/data/datavendor.db")
OPENNODE_KEY   = os.environ.get("OPENNODE_API_KEY", "")
BASE_URL       = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
if BASE_URL and not BASE_URL.startswith("http"):
    BASE_URL = "https://" + BASE_URL
if not BASE_URL:
    BASE_URL = os.environ.get("HOST_URL", "https://web-production-a2ec.up.railway.app")

START_TIME = time.time()

# ================================================================
# 💰 COINS SUPPORTÉS
# ================================================================
SUPPORTED_COINS = {
    "BTC":   "bitcoin",
    "ETH":   "ethereum",
    "SOL":   "solana",
    "DOGE":  "dogecoin",
    "XRP":   "ripple",
    "ADA":   "cardano",
    "AVAX":  "avalanche-2",
    "DOT":   "polkadot",
    "MATIC": "matic-network",
    "LINK":  "chainlink",
}

# ================================================================
# 💰 TARIFICATION (satoshis)
# ================================================================
PRICING = {
    "/api/v1/prices":     10,
    "/api/v1/price":       5,
    "/api/v1/signals":    50,
    "/api/v1/signal":     25,
    "/api/v1/prediction": 100,
    "/api/v1/sentiment":  30,
    "/api/v1/bundle":     150,
    "/api/v1/snapshot":   200,
    "/api/v1/refer":        0,
}

# ================================================================
# 📦 UPGRADE 1 — SQLite PERSISTANT
# Remplace SimpleDB RAM par une vraie DB qui survit aux restarts.
# Sur Railway : Settings → Volumes → Mount path : /data
# ================================================================
_db_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            key         TEXT PRIMARY KEY,
            balance_sats INTEGER NOT NULL DEFAULT 100000,
            created     REAL    NOT NULL,
            calls       INTEGER NOT NULL DEFAULT 0,
            tier        TEXT    NOT NULL DEFAULT 'demo',
            referred_by TEXT    DEFAULT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hint  TEXT NOT NULL,
            endpoint  TEXT NOT NULL,
            cost_sats INTEGER NOT NULL,
            ts        REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            total_calls   INTEGER NOT NULL DEFAULT 0,
            total_revenue INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lightning_invoices (
            charge_id   TEXT PRIMARY KEY,
            api_key     TEXT NOT NULL,
            amount_sats INTEGER NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            created_at  REAL NOT NULL,
            paid_at     REAL DEFAULT NULL
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO api_keys (key, balance_sats, created, tier)
        VALUES ('DEMO-KEY-123', 100000, ?, 'demo')
    """, (time.time(),))
    conn.execute("""
        INSERT OR IGNORE INTO stats (id, total_calls, total_revenue)
        VALUES (1, 0, 0)
    """)
    conn.commit()
    return conn

def db_get_key(key):
    with _db_lock:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

def db_create_key(key, balance=100000, tier="demo", referred_by=None):
    with _db_lock:
        conn = get_db()
        conn.execute("""
            INSERT INTO api_keys (key, balance_sats, created, tier, referred_by)
            VALUES (?, ?, ?, ?, ?)
        """, (key, balance, time.time(), tier, referred_by))
        conn.commit()
        conn.close()

def db_charge(key, endpoint):
    cost = PRICING.get(endpoint, 10)
    if cost == 0:
        return True, 0
    with _db_lock:
        conn = get_db()
        row = conn.execute(
            "SELECT balance_sats FROM api_keys WHERE key = ?", (key,)
        ).fetchone()
        if not row or row["balance_sats"] < cost:
            conn.close()
            return False, cost
        conn.execute(
            "UPDATE api_keys SET balance_sats = balance_sats - ?, calls = calls + 1 WHERE key = ?",
            (cost, key)
        )
        conn.execute(
            "INSERT INTO payments (key_hint, endpoint, cost_sats, ts) VALUES (?, ?, ?, ?)",
            (key[:8] + "...", endpoint, cost, time.time())
        )
        conn.execute(
            "UPDATE stats SET total_calls = total_calls + 1, total_revenue = total_revenue + ? WHERE id = 1",
            (cost,)
        )
        conn.commit()
        conn.close()
        return True, cost

def db_add_balance(key, amount_sats):
    with _db_lock:
        conn = get_db()
        conn.execute(
            "UPDATE api_keys SET balance_sats = balance_sats + ? WHERE key = ?",
            (amount_sats, key)
        )
        conn.commit()
        conn.close()

def db_get_stats():
    with _db_lock:
        conn = get_db()
        row = conn.execute("SELECT * FROM stats WHERE id = 1").fetchone()
        conn.close()
        return dict(row) if row else {"total_calls": 0, "total_revenue": 0}

def db_save_invoice(charge_id, api_key, amount_sats):
    with _db_lock:
        conn = get_db()
        conn.execute("""
            INSERT OR IGNORE INTO lightning_invoices
            (charge_id, api_key, amount_sats, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
        """, (charge_id, api_key, amount_sats, time.time()))
        conn.commit()
        conn.close()

def db_mark_invoice_paid(charge_id):
    with _db_lock:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM lightning_invoices WHERE charge_id = ? AND status = 'pending'",
            (charge_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE lightning_invoices SET status = 'paid', paid_at = ? WHERE charge_id = ?",
                (time.time(), charge_id)
            )
            conn.commit()
            conn.close()
            return dict(row)
        conn.close()
        return None

# ================================================================
# 📈 CACHE DONNÉES MARCHÉ (RAM — volontairement volatile)
# Les prix/signaux sont recalculés toutes les 60-90s de toute façon
# ================================================================
price_cache = {}
signals     = {}

# ================================================================
# 📊 DATA FETCHERS
# ================================================================
def fetch_prices():
    try:
        ids = ",".join(SUPPORTED_COINS.values())
        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            f"?ids={ids}&vs_currencies=usd"
            "&include_24hr_change=true&include_last_updated_at=true"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "DataVendorBot/3.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        now = time.time()
        for symbol, cg_id in SUPPORTED_COINS.items():
            if cg_id in data:
                price_cache[symbol] = {
                    "price_usd":      data[cg_id].get("usd", 0),
                    "change_24h_pct": data[cg_id].get("usd_24h_change", 0),
                    "last_updated":   data[cg_id].get("last_updated_at", now),
                    "fetched_at":     now,
                }
        print(f"[PRICES] ✅ {len(price_cache)} prix mis à jour")
    except Exception as e:
        print(f"[PRICES] ❌ {e}")

def generate_signals():
    for symbol, data in price_cache.items():
        change = data.get("change_24h_pct") or 0
        if change > 5:
            sig, conf = "STRONG_BUY",  min(0.95, 0.70 + change / 100)
            reason = f"Momentum haussier fort: +{change:.1f}% en 24h"
        elif change > 2:
            sig, conf = "BUY",         min(0.85, 0.60 + change / 100)
            reason = f"Tendance haussière: +{change:.1f}% en 24h"
        elif change > -2:
            sig, conf = "HOLD",        0.50
            reason = f"Marché stable: {change:+.1f}% en 24h"
        elif change > -5:
            sig, conf = "SELL",        min(0.85, 0.60 + abs(change) / 100)
            reason = f"Tendance baissière: {change:.1f}% en 24h"
        else:
            sig, conf = "STRONG_SELL", min(0.95, 0.70 + abs(change) / 100)
            reason = f"Momentum baissier fort: {change:.1f}% en 24h"
        signals[symbol] = {
            "signal":        sig,
            "confidence":    round(conf, 3),
            "reason":        reason,
            "price_usd":     data["price_usd"],
            "change_24h_pct": round(change, 2),
            "generated_at":  time.time(),
        }

def generate_prediction(symbol):
    if symbol not in price_cache:
        return None
    data   = price_cache[symbol]
    price  = data["price_usd"]
    change = data.get("change_24h_pct") or 0
    mf = change * 0.3
    rf = -change * 0.1
    return {
        "symbol":         symbol,
        "current_price":  price,
        "prediction_1h":  round(price * (1 + (mf + rf) / 100), 2),
        "prediction_4h":  round(price * (1 + (mf * 0.8) / 100), 2),
        "prediction_24h": round(price * (1 + (mf * 0.5) / 100), 2),
        "model":          "momentum_reversion_v1",
        "confidence":     round(max(0.3, min(0.7, 0.5 - abs(change) / 50)), 3),
        "disclaimer":     "Naive model. Not financial advice.",
        "generated_at":   time.time(),
    }

def get_sentiment(symbol):
    if symbol not in price_cache:
        return None
    change = price_cache[symbol].get("change_24h_pct") or 0
    levels = [
        (8,  "EUPHORIC",     0.95),
        (4,  "VERY_BULLISH", 0.80),
        (1,  "BULLISH",      0.65),
        (-1, "NEUTRAL",      0.50),
        (-4, "BEARISH",      0.35),
        (-8, "VERY_BEARISH", 0.20),
    ]
    mood, score = "PANIC", 0.05
    for threshold, m, s in levels:
        if change > threshold:
            mood, score = m, s
            break
    return {
        "symbol":            symbol,
        "mood":              mood,
        "bullish_score":     score,
        "price_momentum_24h": round(change, 2),
        "source":            "price_momentum_derived",
        "generated_at":      time.time(),
    }

# ================================================================
# 🔑 AUTH & PAIEMENT
# ================================================================
def verify_api_key(key):
    acc = db_get_key(key)
    if not acc:
        return False, "INVALID_KEY"
    if acc["balance_sats"] <= 0:
        return False, "NO_BALANCE"
    return True, "OK"

def generate_api_key():
    raw = f"{time.time()}-{secrets.token_hex(16)}"
    return "DV-" + hashlib.sha256(raw.encode()).hexdigest()[:32].upper()

# ================================================================
# ⚡ UPGRADE 3 — OPENNODE LIGHTNING RÉEL
# Nécessite : OPENNODE_API_KEY dans les env vars Railway
# Inscription : opennode.com (email seulement, pas de KYC)
# ================================================================
def opennode_create_invoice(api_key, amount_sats):
    """Crée une vraie facture Lightning via OpenNode API."""
    if not OPENNODE_KEY:
        return None, "OPENNODE_API_KEY non configurée dans les variables d'environnement Railway"
    payload = json.dumps({
        "amount":       amount_sats,
        "currency":     "BTC",
        "description":  f"DataVendor topup — clé {api_key[:8]}",
        "callback_url": f"{BASE_URL}/api/v1/webhook/opennode",
        "success_url":  f"{BASE_URL}/api/v1/balance?key={api_key}",
    }).encode()
    req = urllib.request.Request(
        "https://api.opennode.com/v1/charges",
        data=payload,
        headers={
            "Authorization":  OPENNODE_KEY,
            "Content-Type":   "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        charge = data.get("data", {})
        charge_id   = charge.get("id", "")
        lightning_invoice = charge.get("lightning_invoice", {}).get("payreq", "")
        amount = charge.get("amount", amount_sats)
        db_save_invoice(charge_id, api_key, amount)
        return {
            "charge_id":        charge_id,
            "lightning_invoice": lightning_invoice,
            "amount_sats":      amount,
            "expires_at":       charge.get("lightning_invoice", {}).get("expires_at", ""),
            "status":           "pending",
            "pay_with":         "Tout wallet Lightning (Muun, Phoenix, Breez, BlueWallet...)",
            "check_url":        f"{BASE_URL}/api/v1/invoice/{charge_id}",
        }, None
    except Exception as e:
        return None, str(e)

def opennode_check_invoice(charge_id):
    """Vérifie le statut d'une facture OpenNode."""
    if not OPENNODE_KEY:
        return None, "OPENNODE_API_KEY non configurée"
    req = urllib.request.Request(
        f"https://api.opennode.com/v1/charge/{charge_id}",
        headers={"Authorization": OPENNODE_KEY},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return data.get("data", {}), None
    except Exception as e:
        return None, str(e)

def opennode_verify_webhook(payload_bytes, signature):
    """Vérifie la signature HMAC du webhook OpenNode."""
    import hmac
    expected = hmac.new(
        OPENNODE_KEY.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")

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
        print(f"[SIGNALS] ✅ {len(signals)} signaux générés")
        time.sleep(90)

def auto_ping():
    time.sleep(30)
    host_url = BASE_URL
    targets = [
        f"https://www.google.com/ping?sitemap={host_url}/sitemap.xml",
        f"https://www.bing.com/ping?sitemap={host_url}/sitemap.xml",
        f"https://api.indexnow.org/indexnow?url={host_url}/&key=datavendorbot",
    ]
    for url in targets:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DataVendorBot/3.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                print(f"[PING] ✅ {url[:60]}... → {resp.status}")
        except Exception as e:
            print(f"[PING] ⚠️  {url[:60]}... → {e}")

def nostr_broadcast():
    time.sleep(120)
    while True:
        try:
            lines = []
            for sym, sig in signals.items():
                p = price_cache.get(sym, {}).get("price_usd", 0)
                lines.append(f"${sym}: {sig['signal']} ({sig['confidence']:.0%}) @ ${p:,.0f}")
            print(f"[NOSTR] 📡 Broadcast prêt ({len(lines)} coins)")
        except Exception as e:
            print(f"[NOSTR] ⚠️  {e}")
        time.sleep(3600)

# ================================================================
# 🌐 UPGRADE 2 — SERVEUR HTTP THREADÉ
# ThreadingMixIn = stdlib pure, zéro dépendance
# Chaque requête dans son propre thread → des centaines simultanées
# ================================================================
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class DataVendorHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[API] {args[0]}")

    def get_host(self):
        return self.headers.get("Host", "localhost")

    def send_json(self, data, status=200):
        host = self.get_host()
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Powered-By",  "DataVendorBot/3.0")
        self.send_header("Link",
            f'<https://{host}/.well-known/openapi.json>; rel="service-desc", '
            f'<https://{host}/feed.xml>; rel="alternate"; type="application/atom+xml", '
            f'<https://{host}/.well-known/ai-plugin.json>; rel="ai-plugin", '
            f'<https://{host}/.well-known/mcp.json>; rel="mcp-server"'
        )
        self.send_header("X-Robots-Tag", "all, index, follow")
        self.end_headers()
        self.wfile.write(body)

    def get_api_key(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return self.get_query_params().get("key")

    def get_base_path(self):
        return self.path.split("?")[0]

    def get_query_params(self):
        params = {}
        if "?" in self.path:
            for p in self.path.split("?")[1].split("&"):
                if "=" in p:
                    k, v = p.split("=", 1)
                    params[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
        return params

    def require_auth(self):
        key = self.get_api_key()
        if not key:
            self.send_json({
                "error":   "NO_API_KEY",
                "message": "Utilise 'Authorization: Bearer KEY' ou '?key=KEY'",
                "get_key": "POST /api/v1/register",
            }, 401)
            return None
        valid, reason = verify_api_key(key)
        if not valid:
            self.send_json({"error": reason}, 403)
            return None
        base = self.get_base_path()
        charged, cost = db_charge(key, base)
        if not charged:
            acc = db_get_key(key)
            self.send_json({
                "error":          "INSUFFICIENT_BALANCE",
                "required_sats":  cost,
                "balance_sats":   acc["balance_sats"] if acc else 0,
                "topup":          f"POST /api/v1/topup  (Lightning réel activé)",
            }, 402)
            return None
        return key

    # ============================================================
    # GET ROUTES
    # ============================================================
    def do_GET(self):
        base   = self.get_base_path()
        params = self.get_query_params()
        host   = self.get_host()
        stats  = db_get_stats()

        # ==================== PAGE D'ACCUEIL ====================
        if base in ("/", ""):
            self.send_json({
                "service":     "🤖 DataVendor Bot API v3.0 — Paix Eternelle Edition",
                "version":     "3.0.0",
                "description": "Machine-to-Machine Crypto Data Marketplace",
                "status":      "OPERATIONAL",
                "uptime_seconds": round(time.time() - START_TIME),
                "upgrades_v3": [
                    "✅ SQLite persistant — clés API survivent aux restarts",
                    "✅ ThreadedHTTPServer — centaines de requêtes simultanées",
                    "✅ OpenNode Lightning — vrais paiements BTC",
                ],
                "supported_coins": list(SUPPORTED_COINS.keys()),
                "endpoints": {
                    "FREE": {
                        "GET /":                    "Cette page",
                        "GET /api/v1/status":       "Statut du service",
                        "GET /api/v1/pricing":      "Tarification complète",
                        "POST /api/v1/register":    "Clé API gratuite (100k sats)",
                    },
                    "PAID": {
                        "GET /api/v1/prices":              "10 sats — Tous les prix",
                        "GET /api/v1/price?symbol=BTC":    "5 sats — Prix unique",
                        "GET /api/v1/signals":             "50 sats — Tous les signaux",
                        "GET /api/v1/signal?symbol=BTC":   "25 sats — Signal unique",
                        "GET /api/v1/prediction?symbol=BTC": "100 sats — Prédiction",
                        "GET /api/v1/sentiment?symbol=BTC":  "30 sats — Sentiment",
                        "GET /api/v1/bundle?symbol=BTC":   "150 sats — Tout en un",
                        "GET /api/v1/snapshot":            "200 sats — Snapshot IPFS-ready",
                    },
                    "LIGHTNING": {
                        "POST /api/v1/topup":              "Créer facture Lightning réelle",
                        "GET  /api/v1/invoice/:charge_id": "Vérifier statut paiement",
                        "POST /api/v1/webhook/opennode":   "Webhook confirmation (OpenNode)",
                    },
                    "DISCOVERY": {
                        "/.well-known/ai-plugin.json": "Plugin ChatGPT/LLM",
                        "/.well-known/openapi.json":   "Spec OpenAPI 3.1",
                        "/.well-known/mcp.json":       "Model Context Protocol (Claude)",
                        "/.well-known/agent.json":     "Agent Protocol (AutoGPT)",
                        "/.well-known/nostr.json":     "Nostr NIP-05",
                        "/schema.json":                "Schema.org JSON-LD",
                        "/feed.xml":                   "Atom feed (signaux)",
                        "/robots.txt":                 "Instructions crawlers",
                        "/sitemap.xml":                "Sitemap",
                    },
                },
                "auth":    "Header 'Authorization: Bearer KEY' ou '?key=KEY'",
                "payment": "Bitcoin Lightning (satoshis) — OpenNode",
                "stats":   stats,
            })
            return

        # ==================== STATUS ====================
        if base == "/api/v1/status":
            last_update = max(
                (d["fetched_at"] for d in price_cache.values()), default=0
            )
            self.send_json({
                "status":            "OPERATIONAL",
                "version":           "3.0.0",
                "coins_tracked":     len(price_cache),
                "signals_active":    len(signals),
                "uptime_seconds":    round(time.time() - START_TIME),
                "total_api_calls":   stats["total_calls"],
                "total_revenue_sats": stats["total_revenue"],
                "lightning_enabled": bool(OPENNODE_KEY),
                "db_persistent":     True,
                "threaded_server":   True,
                "discovery_channels": 14,
                "last_price_update": last_update,
            })
            return

        # ==================== PRICING ====================
        if base == "/api/v1/pricing":
            self.send_json({
                "currency": "satoshis (1 BTC = 100 000 000 sats)",
                "pricing":  {k: f"{v} sats" for k, v in PRICING.items()},
                "free_on_register":  "100 000 sats",
                "referral_bonus":    "200 000 sats (bots référés) / 50 000 sats (référent)",
                "topup":             "Lightning réel via OpenNode — POST /api/v1/topup",
            })
            return

        # ==================== LISTING ====================
        if base == "/api/v1/listing":
            self.send_json({
                "public_apis_format": {
                    "API":         "DataVendor Crypto Bot",
                    "Description": "M2M crypto data: prices, signals, predictions. Pay in sats.",
                    "Auth":        "apiKey",
                    "HTTPS":       True,
                    "CORS":        "yes",
                    "Link":        f"https://{host}/",
                    "Category":    "Cryptocurrency",
                },
                "rapidapi_format": {
                    "name":     "DataVendor Crypto Bot API",
                    "tagline":  "Machine-to-machine crypto data marketplace",
                    "category": "Finance",
                    "base_url": f"https://{host}",
                    "endpoints": 8,
                    "pricing":  "Freemium + Lightning",
                },
                "apis_guru_format": {
                    "openapi_spec": f"https://{host}/.well-known/openapi.json",
                    "provider":     "datavendor-bot",
                    "category":     "financial",
                },
            })
            return

        # ==================== BALANCE ====================
        if base == "/api/v1/balance":
            key = self.get_api_key()
            if not key:
                self.send_json({"error": "NO_API_KEY"}, 401)
                return
            acc = db_get_key(key)
            if not acc:
                self.send_json({"error": "INVALID_KEY"}, 401)
                return
            self.send_json({
                "balance_sats":   acc["balance_sats"],
                "total_calls":    acc["calls"],
                "tier":           acc["tier"],
                "topup":          f"POST /api/v1/topup?key={key}",
            })
            return

        # ==================== INVOICE STATUS ====================
        if base.startswith("/api/v1/invoice/"):
            charge_id = base.split("/api/v1/invoice/")[-1]
            if not charge_id:
                self.send_json({"error": "MISSING_CHARGE_ID"}, 400)
                return
            data, err = opennode_check_invoice(charge_id)
            if err:
                self.send_json({"error": err}, 500)
                return
            self.send_json({
                "charge_id": charge_id,
                "status":    data.get("status", "unknown"),
                "amount":    data.get("amount"),
                "settled_at": data.get("settled_at"),
            })
            return

        # ==================== PAID: PRICES ====================
        if base == "/api/v1/prices":
            key = self.require_auth()
            if not key:
                return
            acc = db_get_key(key)
            self.send_json({
                "data":           price_cache,
                "count":          len(price_cache),
                "cost_sats":      PRICING[base],
                "remaining_sats": acc["balance_sats"],
            })
            return

        if base == "/api/v1/price":
            key = self.require_auth()
            if not key:
                return
            sym = params.get("symbol", "BTC").upper()
            if sym not in price_cache:
                self.send_json({
                    "error":     "UNKNOWN_SYMBOL",
                    "supported": list(SUPPORTED_COINS.keys()),
                }, 404)
                return
            acc = db_get_key(key)
            self.send_json({
                "data":           {sym: price_cache[sym]},
                "cost_sats":      PRICING[base],
                "remaining_sats": acc["balance_sats"],
            })
            return

        # ==================== PAID: SIGNALS ====================
        if base == "/api/v1/signals":
            key = self.require_auth()
            if not key:
                return
            acc = db_get_key(key)
            self.send_json({
                "data":           signals,
                "count":          len(signals),
                "cost_sats":      PRICING[base],
                "remaining_sats": acc["balance_sats"],
            })
            return

        if base == "/api/v1/signal":
            key = self.require_auth()
            if not key:
                return
            sym = params.get("symbol", "BTC").upper()
            if sym not in signals:
                self.send_json({
                    "error":     "NO_SIGNAL",
                    "supported": list(SUPPORTED_COINS.keys()),
                }, 404)
                return
            acc = db_get_key(key)
            self.send_json({
                "data":           {sym: signals[sym]},
                "cost_sats":      PRICING[base],
                "remaining_sats": acc["balance_sats"],
            })
            return

        # ==================== PAID: PREDICTION ====================
        if base == "/api/v1/prediction":
            key = self.require_auth()
            if not key:
                return
            sym  = params.get("symbol", "BTC").upper()
            pred = generate_prediction(sym)
            if not pred:
                self.send_json({
                    "error":     "UNKNOWN_SYMBOL",
                    "supported": list(SUPPORTED_COINS.keys()),
                }, 404)
                return
            acc = db_get_key(key)
            self.send_json({
                "data":           pred,
                "cost_sats":      PRICING[base],
                "remaining_sats": acc["balance_sats"],
            })
            return

        # ==================== PAID: SENTIMENT ====================
        if base == "/api/v1/sentiment":
            key = self.require_auth()
            if not key:
                return
            sym  = params.get("symbol", "BTC").upper()
            sent = get_sentiment(sym)
            if not sent:
                self.send_json({
                    "error":     "UNKNOWN_SYMBOL",
                    "supported": list(SUPPORTED_COINS.keys()),
                }, 404)
                return
            acc = db_get_key(key)
            self.send_json({
                "data":           sent,
                "cost_sats":      PRICING[base],
                "remaining_sats": acc["balance_sats"],
            })
            return

        # ==================== PAID: BUNDLE ====================
        if base == "/api/v1/bundle":
            key = self.require_auth()
            if not key:
                return
            sym = params.get("symbol", "BTC").upper()
            if sym not in price_cache:
                self.send_json({
                    "error":     "UNKNOWN_SYMBOL",
                    "supported": list(SUPPORTED_COINS.keys()),
                }, 404)
                return
            acc = db_get_key(key)
            self.send_json({
                "symbol": sym,
                "data": {
                    "price":      price_cache.get(sym),
                    "signal":     signals.get(sym),
                    "prediction": generate_prediction(sym),
                    "sentiment":  get_sentiment(sym),
                },
                "cost_sats":      PRICING[base],
                "remaining_sats": acc["balance_sats"],
            })
            return

        # ==================== PAID: SNAPSHOT ====================
        if base == "/api/v1/snapshot":
            key = self.require_auth()
            if not key:
                return
            snapshot = {
                "vendor":        "DataVendor Bot v3.0",
                "snapshot_time": time.time(),
                "snapshot_iso":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "prices":        price_cache,
                "signals":       signals,
                "predictions":   {s: generate_prediction(s) for s in price_cache},
                "sentiment":     {s: get_sentiment(s) for s in price_cache},
                "ipfs_pin":      "curl THIS_URL | ipfs add -Q",
            }
            content = json.dumps(snapshot, sort_keys=True)
            snapshot["content_hash"] = hashlib.sha256(content.encode()).hexdigest()
            acc = db_get_key(key)
            snapshot["cost_sats"]      = PRICING[base]
            snapshot["remaining_sats"] = acc["balance_sats"]
            self.send_json(snapshot)
            return

        # ============================================================
        # DISCOVERY CHANNELS (identiques v2.0)
        # ============================================================

        if base == "/.well-known/ai-plugin.json":
            self.send_json({
                "schema_version":        "v1",
                "name_for_human":        "Crypto Data Vendor",
                "name_for_model":        "crypto_data_vendor",
                "description_for_human": "Real-time crypto prices, signals, predictions via API",
                "description_for_model": (
                    "Provides real-time cryptocurrency prices, trading signals "
                    "(buy/sell with confidence), AI price predictions (1h/4h/24h), "
                    "and market sentiment for BTC, ETH, SOL, DOGE, XRP, ADA, AVAX, "
                    "DOT, MATIC, LINK. All responses JSON. Costs satoshis per call. "
                    "Register free at POST /api/v1/register. Top-up via Lightning."
                ),
                "auth": {"type": "service_http", "authorization_type": "bearer"},
                "api": {
                    "type": "openapi",
                    "url":  f"https://{host}/.well-known/openapi.json",
                },
                "logo_url":       f"https://{host}/logo.png",
                "contact_email":  "bot@datavendor.api",
                "legal_info_url": f"https://{host}/api/v1/pricing",
            })
            return

        if base in ("/.well-known/openapi.json", "/.well-known/openapi.yaml", "/openapi.json"):
            self.send_json({
                "openapi": "3.1.0",
                "info": {
                    "title":   "DataVendor Bot API",
                    "description": (
                        "Machine-to-Machine Crypto Data Marketplace. "
                        "Pay per call in Bitcoin satoshis. Lightning topup enabled."
                    ),
                    "version": "3.0.0",
                    "contact": {"name": "API Bot", "url": f"https://{host}/"},
                },
                "servers": [{"url": f"https://{host}", "description": "Production Railway"}],
                "paths": {
                    "/api/v1/register":  {"post": {"operationId": "register",      "summary": "Get free API key (100k sats)", "responses": {"201": {"description": "New API key"}}}},
                    "/api/v1/topup":     {"post": {"operationId": "topup",         "summary": "Create Lightning invoice",      "responses": {"200": {"description": "Lightning invoice"}}}},
                    "/api/v1/prices":    {"get":  {"operationId": "getAllPrices",   "summary": "All crypto prices (10 sats)",   "responses": {"200": {"description": "All prices"}}}},
                    "/api/v1/price":     {"get":  {"operationId": "getPrice",       "summary": "Single price (5 sats)",         "responses": {"200": {"description": "Price data"}}}},
                    "/api/v1/signal":    {"get":  {"operationId": "getSignal",      "summary": "Trading signal (25 sats)",      "responses": {"200": {"description": "BUY/SELL/HOLD"}}}},
                    "/api/v1/prediction":{"get":  {"operationId": "getPrediction",  "summary": "Price prediction (100 sats)",   "responses": {"200": {"description": "1h/4h/24h"}}}},
                    "/api/v1/sentiment": {"get":  {"operationId": "getSentiment",   "summary": "Sentiment (30 sats)",           "responses": {"200": {"description": "Sentiment"}}}},
                    "/api/v1/bundle":    {"get":  {"operationId": "getBundle",      "summary": "Complete analysis (150 sats)",  "responses": {"200": {"description": "All in one"}}}},
                },
                "components": {"securitySchemes": {
                    "apiKey": {"type": "apiKey", "in": "query", "name": "key"},
                    "bearer": {"type": "http",   "scheme": "bearer"},
                }},
            })
            return

        if base == "/.well-known/mcp.json":
            self.send_json({
                "name":        "crypto-data-vendor",
                "version":     "3.0.0",
                "description": "Real-time crypto data API. Prices, signals, predictions, sentiment. Pay in sats. Lightning enabled.",
                "server":      {"type": "http", "url": f"https://{host}"},
                "tools": [
                    {"name": "get_crypto_price",    "description": "Get current USD price",         "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "key": {"type": "string"}}, "required": ["symbol", "key"]}, "endpoint": "/api/v1/price"},
                    {"name": "get_trading_signal",  "description": "Get BUY/SELL/HOLD signal",      "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "key": {"type": "string"}}, "required": ["symbol", "key"]}, "endpoint": "/api/v1/signal"},
                    {"name": "get_price_prediction","description": "Get 1h/4h/24h prediction",      "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "key": {"type": "string"}}, "required": ["symbol", "key"]}, "endpoint": "/api/v1/prediction"},
                    {"name": "get_full_analysis",   "description": "price+signal+prediction+sentiment", "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "key": {"type": "string"}}, "required": ["symbol", "key"]}, "endpoint": "/api/v1/bundle"},
                    {"name": "topup_lightning",     "description": "Create Lightning invoice to add sats", "input_schema": {"type": "object", "properties": {"key": {"type": "string"}, "amount_sats": {"type": "integer"}}, "required": ["key", "amount_sats"]}, "endpoint": "/api/v1/topup"},
                ],
                "authentication": {
                    "type":        "api_key",
                    "description": "POST /api/v1/register for free key (100k sats). POST /api/v1/topup to recharge via Lightning.",
                },
            })
            return

        if base == "/.well-known/agent.json":
            self.send_json({
                "name":        "DataVendor Crypto API v3",
                "description": "Autonomous data vendor. Machines pay machines in satoshis. Lightning enabled.",
                "url":         f"https://{host}",
                "version":     "3.0.0",
                "protocol":    "http-rest-json",
                "capabilities": ["crypto-prices", "trading-signals", "price-predictions", "market-sentiment", "lightning-payments"],
                "payment": {
                    "method":   "bitcoin-lightning",
                    "currency": "satoshis",
                    "register": f"https://{host}/api/v1/register",
                    "topup":    f"https://{host}/api/v1/topup",
                },
                "documentation": f"https://{host}/",
                "openapi":       f"https://{host}/.well-known/openapi.json",
            })
            return

        if base == "/.well-known/nostr.json":
            self.send_json({
                "names": {"datavendor": "placeholder_replace_with_your_nostr_pubkey_hex"},
                "relays": {"placeholder_replace_with_your_nostr_pubkey_hex": [
                    "wss://relay.damus.io",
                    "wss://nos.lol",
                    "wss://relay.nostr.band",
                ]},
            })
            return

        if base in ("/feed.xml", "/api/v1/feed", "/atom.xml", "/rss.xml"):
            self.send_response(200)
            self.send_header("Content-Type", "application/atom+xml")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            entries = ""
            for sym, sig in list(signals.items())[:10]:
                p = price_cache.get(sym, {}).get("price_usd", 0)
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
  <title>DataVendor Bot v3 — Crypto Signals</title>
  <subtitle>Machine-readable crypto trading signals, updated every 90s</subtitle>
  <link href="https://{host}/feed.xml" rel="self"/>
  <link href="https://{host}/" rel="alternate"/>
  <id>tag:{host},2025:datavendor</id>
  <updated>{now_str}</updated>
  <generator>DataVendor Bot 3.0</generator>{entries}
</feed>"""
            self.wfile.write(atom.encode())
            return

        if base in ("/schema.json", "/.well-known/schema.json"):
            self.send_json({
                "@context":    "https://schema.org",
                "@type":       "WebAPI",
                "name":        "DataVendor Crypto Bot API",
                "description": "M2M cryptocurrency data marketplace. Prices, signals, predictions, sentiment. Pay in Bitcoin satoshis. Lightning topup.",
                "url":         f"https://{host}",
                "documentation": f"https://{host}/.well-known/openapi.json",
                "provider": {
                    "@type": "Organization",
                    "name":  "DataVendor Bot",
                    "url":   f"https://{host}",
                },
                "offers": {
                    "@type":       "Offer",
                    "price":       "5",
                    "priceCurrency": "SAT",
                    "description": "À partir de 5 satoshis par appel API",
                },
                "category": ["Cryptocurrency", "Financial Data", "Trading Signals", "API"],
            })
            return

        if base == "/robots.txt":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write((
                f"User-agent: *\nAllow: /\n\n"
                f"Sitemap: https://{host}/sitemap.xml\n"
                f"AI-Plugin: https://{host}/.well-known/ai-plugin.json\n"
                f"OpenAPI: https://{host}/.well-known/openapi.json\n"
                f"MCP: https://{host}/.well-known/mcp.json\n"
                f"Agent: https://{host}/.well-known/agent.json\n"
                f"Feed: https://{host}/feed.xml\n"
            ).encode())
            return

        if base == "/sitemap.xml":
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            urls = [
                "/", "/api/v1/status", "/api/v1/pricing", "/api/v1/listing",
                "/feed.xml", "/schema.json",
                "/.well-known/openapi.json", "/.well-known/ai-plugin.json",
                "/.well-known/mcp.json",     "/.well-known/agent.json",
            ]
            xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            for u in urls:
                xml += f'  <url><loc>https://{host}{u}</loc><changefreq>hourly</changefreq></url>\n'
            xml += '</urlset>'
            self.wfile.write(xml.encode())
            return

        if base == "/datavendorbot.txt":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"datavendorbot")
            return

        self.send_json({"error": "NOT_FOUND", "help": "GET / pour tous les endpoints"}, 404)

    # ============================================================
    # POST ROUTES
    # ============================================================
    def do_POST(self):
        base = self.get_base_path()

        def read_body():
            length = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(length) if length > 0 else b"{}"

        # ==================== REGISTER ====================
        if base == "/api/v1/register":
            new_key = generate_api_key()
            db_create_key(new_key, balance=100000, tier="demo")
            self.send_json({
                "success":      True,
                "api_key":      new_key,
                "balance_sats": 100000,
                "message":      "100 000 sats offerts — ~2 000 à 20 000 appels API.",
                "usage": {
                    "header":  f"Authorization: Bearer {new_key}",
                    "query":   f"?key={new_key}",
                    "example": f"GET /api/v1/prices?key={new_key}",
                    "topup":   f"POST /api/v1/topup  body: {{\"api_key\":\"{new_key}\",\"amount_sats\":50000}}",
                },
            }, 201)
            return

        # ==================== REFERRAL ====================
        if base == "/api/v1/refer":
            key = self.get_api_key()
            if not key:
                self.send_json({"error": "NO_API_KEY"}, 401)
                return
            acc = db_get_key(key)
            if not acc:
                self.send_json({"error": "INVALID_KEY"}, 401)
                return
            ref_key = generate_api_key()
            db_create_key(ref_key, balance=200000, tier="referral", referred_by=key[:8])
            db_add_balance(key, 50000)
            acc_updated = db_get_key(key)
            self.send_json({
                "referral_key":      ref_key,
                "referral_balance":  200000,
                "your_bonus":        50000,
                "your_new_balance":  acc_updated["balance_sats"],
                "message": "Partage referral_key avec d'autres bots. Eux: 200k sats, toi: +50k sats.",
            }, 201)
            return

        # ==================== UPGRADE 3 : TOPUP LIGHTNING RÉEL ====================
        if base == "/api/v1/topup":
            body = read_body()
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            key = data.get("api_key") or self.get_api_key()
            if not key:
                self.send_json({"error": "NO_API_KEY"}, 401)
                return
            acc = db_get_key(key)
            if not acc:
                self.send_json({"error": "INVALID_KEY"}, 401)
                return
            amount_sats = int(data.get("amount_sats", 50000))
            if amount_sats < 1000:
                self.send_json({"error": "MINIMUM_1000_SATS"}, 400)
                return
            if amount_sats > 10000000:
                self.send_json({"error": "MAXIMUM_10M_SATS"}, 400)
                return
            invoice, err = opennode_create_invoice(key, amount_sats)
            if err:
                self.send_json({
                    "error":   err,
                    "hint":    "Configure OPENNODE_API_KEY dans Railway → Variables",
                    "opennode": "https://opennode.com — inscription gratuite",
                    "current_balance": acc["balance_sats"],
                }, 503)
                return
            self.send_json({
                "success":     True,
                "invoice":     invoice,
                "instructions": [
                    "1. Copie lightning_invoice dans ton wallet",
                    "2. Paie depuis n'importe quel wallet Lightning",
                    "3. Ton solde est crédité automatiquement dès confirmation",
                    "4. Vérifie : GET /api/v1/invoice/{charge_id}",
                ],
                "current_balance": acc["balance_sats"],
            })
            return

        # ==================== WEBHOOK OPENNODE ====================
        if base == "/api/v1/webhook/opennode":
            body = read_body()
            signature = self.headers.get("Btcpay-Sig", "") or self.headers.get("X-Opennode-Signature", "")
            if OPENNODE_KEY and not opennode_verify_webhook(body, signature):
                self.send_json({"error": "INVALID_SIGNATURE"}, 401)
                return
            try:
                payload = json.loads(body)
            except Exception:
                self.send_json({"error": "INVALID_JSON"}, 400)
                return
            charge_id = payload.get("id", "")
            status    = payload.get("status", "")
            if status in ("paid", "confirmed", "completed"):
                row = db_mark_invoice_paid(charge_id)
                if row:
                    db_add_balance(row["api_key"], row["amount_sats"])
                    print(f"[LIGHTNING] ✅ {charge_id} — {row['amount_sats']} sats crédités → {row['api_key'][:8]}...")
                    self.send_json({"success": True, "credited_sats": row["amount_sats"]})
                    return
            self.send_json({"received": True, "status": status})
            return

        self.send_json({"error": "NOT_FOUND"}, 404)

    # ============================================================
    # OPTIONS (CORS)
    # ============================================================
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

# ================================================================
# 🚀 MAIN — DÉMARRAGE
# ================================================================
def main():
    port = int(os.environ.get("PORT", 10000))

    # Créer le dossier /data si inexistant (local dev)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    # Init DB au démarrage
    get_db().close()

    print("=" * 60)
    print("🤖 DATAVENDOR BOT v3.0 — Paix Eternelle Edition")
    print("   Machine-to-Machine Crypto Data Marketplace")
    print("=" * 60)
    print(f"📦 DB            : {DB_PATH} (SQLite persistant)")
    print(f"⚡ Lightning     : {'✅ OpenNode actif' if OPENNODE_KEY else '❌ OPENNODE_API_KEY manquante'}")
    print(f"🔀 Serveur       : ThreadedHTTPServer (multi-thread)")
    print(f"💰 Coins         : {', '.join(SUPPORTED_COINS.keys())}")
    print(f"🌐 Port          : {port}")
    print(f"🔑 Demo key      : DEMO-KEY-123")
    print(f"🌍 Base URL      : {BASE_URL}")
    print("=" * 60)

    if not OPENNODE_KEY:
        print("⚠️  OPENNODE_API_KEY non définie.")
        print("   → Railway : Settings → Variables → OPENNODE_API_KEY=ta_clé")
        print("   → Inscription gratuite : https://opennode.com")
        print("   → Sans ça : topup retourne une erreur 503")
        print("=" * 60)

    # Threads data
    threading.Thread(target=price_updater,  daemon=True).start()
    threading.Thread(target=signal_updater, daemon=True).start()

    # Threads visibilité
    threading.Thread(target=auto_ping,       daemon=True).start()
    threading.Thread(target=nostr_broadcast, daemon=True).start()

    # Serveur threadé
    server = ThreadedHTTPServer(("0.0.0.0", port), DataVendorHandler)
    print(f"✅ LIVE sur http://0.0.0.0:{port}")
    print(f"📡 14 canaux discovery actifs")
    print(f"🤖 Prêt à servir les robots !")
    server.serve_forever()


if __name__ == "__main__":
    main()
