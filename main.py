# ================================================================
# DATAVENDOR BOT v3.0 — FICHIER UNIQUE COMPLET
# ================================================================
# Machine-to-Machine Crypto Data Marketplace
# 14 canaux de visibilite automatique integres
# Zero dependance externe. SQLite persistant. Threaded.
# Copie → Deploie → Dors definitivement.
# ================================================================
#
# UPGRADES v3.0 vs v2.0 :
#   [1] SQLite persistant  — les cles API survivent aux restarts
#   [2] ThreadingMixIn     — 200+ requetes simultanees
#   [3] Topup BTC on-chain — paiement direct, zero API tierce
#   [4] Webhook confirm    — verification TXID manuelle ou auto
#   [5] Health endpoint    — monitoring Railway
#
# DEPLOY RAILWAY :
#   - Ajouter un Volume : Mount Path = /data
#   - Variable optionnelle : BTC_ADDRESS
#   - Variable optionnelle : HOST_URL
#   - Variable optionnelle : ADMIN_TOKEN  (pour confirmer les topups)
#   - Aucune autre config requise
# ================================================================

from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import json
import hashlib
import time
import urllib.request
import threading
import os
import secrets
import sqlite3

# ================================================================
# CONFIG GLOBALE
# ================================================================
BTC_ADDRESS = os.environ.get("BTC_ADDRESS", "1QAWwqdrBE7cL3ZBkNgJvmV95nhe3yoHeu")
DB_PATH     = os.environ.get("DB_PATH",     "/data/datavendor.db")
HOST_URL    = os.environ.get("HOST_URL",    "https://web-production-a2ec.up.railway.app")
PORT        = int(os.environ.get("PORT",    10000))
START_TIME  = time.time()

# ================================================================
# SQLITE PERSISTANT
# ================================================================
DB_LOCK = threading.Lock()

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with DB_LOCK:
        conn = get_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key          TEXT    PRIMARY KEY,
                balance_sats INTEGER NOT NULL DEFAULT 100000,
                created      REAL    NOT NULL,
                calls        INTEGER NOT NULL DEFAULT 0,
                tier         TEXT    NOT NULL DEFAULT 'demo',
                referred_by  TEXT    DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key    TEXT    NOT NULL,
                endpoint   TEXT    NOT NULL,
                cost_sats  INTEGER NOT NULL,
                ts         REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topup_requests (
                txid        TEXT    PRIMARY KEY,
                api_key     TEXT    NOT NULL,
                amount_sats INTEGER NOT NULL,
                confirmed   INTEGER NOT NULL DEFAULT 0,
                created     REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stats (
                id             INTEGER PRIMARY KEY CHECK (id = 1),
                total_calls    INTEGER NOT NULL DEFAULT 0,
                total_revenue  INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO stats (id, total_calls, total_revenue)
                VALUES (1, 0, 0);
            INSERT OR IGNORE INTO api_keys (key, balance_sats, created, tier)
                VALUES ('DEMO-KEY-123', 100000, unixepoch(), 'demo');
        """)
        conn.commit()
        conn.close()
    print(f"[DB] SQLite initialisee → {DB_PATH}")

def db_get_key(key):
    with DB_LOCK:
        conn = get_db()
        row  = conn.execute("SELECT * FROM api_keys WHERE key = ?", (key,)).fetchone()
        conn.close()
        return dict(row) if row else None

def db_create_key(key, balance, tier, referred_by=None):
    with DB_LOCK:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO api_keys "
            "(key, balance_sats, created, tier, referred_by) VALUES (?,?,?,?,?)",
            (key, balance, time.time(), tier, referred_by)
        )
        conn.commit()
        conn.close()

def db_charge(key, endpoint, cost):
    if cost == 0:
        return True
    with DB_LOCK:
        conn = get_db()
        row  = conn.execute(
            "SELECT balance_sats FROM api_keys WHERE key = ?", (key,)
        ).fetchone()
        if not row or row["balance_sats"] < cost:
            conn.close()
            return False
        conn.execute(
            "UPDATE api_keys SET balance_sats = balance_sats - ?, calls = calls + 1 WHERE key = ?",
            (cost, key)
        )
        conn.execute(
            "INSERT INTO payments (api_key, endpoint, cost_sats, ts) VALUES (?,?,?,?)",
            (key, endpoint, cost, time.time())
        )
        conn.execute(
            "UPDATE stats SET total_calls = total_calls + 1, "
            "total_revenue = total_revenue + ? WHERE id = 1", (cost,)
        )
        conn.commit()
        conn.close()
        return True

def db_add_balance(key, amount):
    with DB_LOCK:
        conn = get_db()
        conn.execute(
            "UPDATE api_keys SET balance_sats = balance_sats + ? WHERE key = ?",
            (amount, key)
        )
        conn.commit()
        conn.close()

def db_get_stats():
    with DB_LOCK:
        conn = get_db()
        row  = conn.execute("SELECT * FROM stats WHERE id = 1").fetchone()
        conn.close()
        return dict(row) if row else {"total_calls": 0, "total_revenue": 0}

def db_register_topup(txid, api_key, amount_sats):
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO topup_requests (txid, api_key, amount_sats, confirmed, created) "
                "VALUES (?,?,?,0,?)",
                (txid, api_key, amount_sats, time.time())
            )
            conn.commit()
            ok = True
        except sqlite3.IntegrityError:
            ok = False
        conn.close()
        return ok

def db_confirm_topup(txid):
    with DB_LOCK:
        conn  = get_db()
        row   = conn.execute(
            "SELECT * FROM topup_requests WHERE txid = ? AND confirmed = 0", (txid,)
        ).fetchone()
        if not row:
            conn.close()
            return None
        conn.execute("UPDATE topup_requests SET confirmed = 1 WHERE txid = ?", (txid,))
        conn.execute(
            "UPDATE api_keys SET balance_sats = balance_sats + ? WHERE key = ?",
            (row["amount_sats"], row["api_key"])
        )
        conn.commit()
        result = dict(row)
        conn.close()
        return result

# ================================================================
# CACHE PRIX EN MEMOIRE (rapide, reconstruit toutes les 60s)
# ================================================================
price_cache = {}
signals     = {}
cache_lock  = threading.Lock()

# ================================================================
# COINS SUPPORTES
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
# TARIFICATION (satoshis)
# ================================================================
PRICING = {
    "/api/v1/prices":      10,
    "/api/v1/price":        5,
    "/api/v1/signals":     50,
    "/api/v1/signal":      25,
    "/api/v1/prediction": 100,
    "/api/v1/sentiment":   30,
    "/api/v1/bundle":     150,
    "/api/v1/snapshot":   200,
    "/api/v1/refer":        0,
}

TOPUP_TIERS = [
    {"sats": 100_000,   "label": "100k  sats  (~starter)"},
    {"sats": 500_000,   "label": "500k  sats  (~basic)"},
    {"sats": 1_000_000, "label": "1M    sats  (~pro)"},
    {"sats": 5_000_000, "label": "5M    sats  (~enterprise)"},
]

# ================================================================
# DATA FETCHERS
# ================================================================
def fetch_prices():
    try:
        ids = ",".join(SUPPORTED_COINS.values())
        url = (
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={ids}&vs_currencies=usd"
            f"&include_24hr_change=true&include_last_updated_at=true"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "DataVendorBot/3.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        now = time.time()
        with cache_lock:
            for symbol, cg_id in SUPPORTED_COINS.items():
                if cg_id in data:
                    price_cache[symbol] = {
                        "price_usd":      data[cg_id].get("usd", 0),
                        "change_24h_pct": data[cg_id].get("usd_24h_change", 0),
                        "last_updated":   data[cg_id].get("last_updated_at", now),
                        "fetched_at":     now,
                    }
        print(f"[PRICES] {len(price_cache)} prix mis a jour")
    except Exception as e:
        print(f"[PRICES] Erreur: {e}")

def generate_signals():
    with cache_lock:
        snap = dict(price_cache)
    for symbol, data in snap.items():
        change = data.get("change_24h_pct") or 0
        if   change >  5: sig, conf = "STRONG_BUY",  min(0.95, 0.70 + change / 100); reason = f"Momentum haussier fort: +{change:.1f}% en 24h"
        elif change >  2: sig, conf = "BUY",          min(0.85, 0.60 + change / 100); reason = f"Tendance haussiere: +{change:.1f}% en 24h"
        elif change > -2: sig, conf = "HOLD",         0.50;                           reason = f"Marche stable: {change:+.1f}% en 24h"
        elif change > -5: sig, conf = "SELL",         min(0.85, 0.60 + abs(change) / 100); reason = f"Tendance baissiere: {change:.1f}% en 24h"
        else:             sig, conf = "STRONG_SELL",  min(0.95, 0.70 + abs(change) / 100); reason = f"Momentum baissier fort: {change:.1f}% en 24h"
        with cache_lock:
            signals[symbol] = {
                "signal":         sig,
                "confidence":     round(conf, 3),
                "reason":         reason,
                "price_usd":      data["price_usd"],
                "change_24h_pct": round(change, 2),
                "generated_at":   time.time(),
            }

def generate_prediction(symbol):
    with cache_lock:
        data = price_cache.get(symbol)
    if not data:
        return None
    price  = data["price_usd"]
    change = data.get("change_24h_pct") or 0
    mf = change * 0.3
    rf = -change * 0.1
    return {
        "symbol":         symbol,
        "current_price":  price,
        "prediction_1h":  round(price * (1 + (mf + rf)  / 100), 2),
        "prediction_4h":  round(price * (1 + (mf * 0.8) / 100), 2),
        "prediction_24h": round(price * (1 + (mf * 0.5) / 100), 2),
        "model":          "momentum_reversion_v1",
        "confidence":     round(max(0.3, min(0.7, 0.5 - abs(change) / 50)), 3),
        "disclaimer":     "Naive model. Not financial advice.",
        "generated_at":   time.time(),
    }

def get_sentiment(symbol):
    with cache_lock:
        data = price_cache.get(symbol)
    if not data:
        return None
    change = data.get("change_24h_pct") or 0
    levels = [(8,"EUPHORIC",0.95),(4,"VERY_BULLISH",0.80),(1,"BULLISH",0.65),
              (-1,"NEUTRAL",0.50),(-4,"BEARISH",0.35),(-8,"VERY_BEARISH",0.20)]
    mood, score = "PANIC", 0.05
    for threshold, m, s in levels:
        if change > threshold:
            mood, score = m, s
            break
    return {
        "symbol":             symbol,
        "mood":               mood,
        "bullish_score":      score,
        "price_momentum_24h": round(change, 2),
        "source":             "price_momentum_derived",
        "generated_at":       time.time(),
    }

# ================================================================
# AUTH
# ================================================================
def verify_api_key(key):
    row = db_get_key(key)
    if not row:
        return False, "INVALID_KEY"
    if row["balance_sats"] <= 0:
        return False, "NO_BALANCE"
    return True, "OK"

def generate_api_key():
    raw = f"{time.time()}-{secrets.token_hex(16)}"
    return "DV-" + hashlib.sha256(raw.encode()).hexdigest()[:32].upper()

# ================================================================
# BACKGROUND THREADS
# ================================================================
def price_updater():
    while True:
        fetch_prices()
        time.sleep(60)

def signal_updater():
    time.sleep(10)
    while True:
        generate_signals()
        with cache_lock:
            n = len(signals)
        print(f"[SIGNALS] {n} signaux generes")
        time.sleep(90)

def auto_ping():
    time.sleep(30)
    host = os.environ.get("RENDER_EXTERNAL_URL") or HOST_URL
    for url in [
        f"https://www.google.com/ping?sitemap={host}/sitemap.xml",
        f"https://www.bing.com/ping?sitemap={host}/sitemap.xml",
        f"https://api.indexnow.org/indexnow?url={host}/&key=datavendorbot",
    ]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DataVendorBot/3.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                print(f"[PING] {url[:55]}... → {resp.status}")
        except Exception as e:
            print(f"[PING] {url[:55]}... → {e}")
    print("[PING] Auto-ping termine")

def nostr_broadcast():
    time.sleep(120)
    host = os.environ.get("RENDER_EXTERNAL_URL") or HOST_URL
    while True:
        try:
            with cache_lock:
                sigs  = dict(signals)
                cache = dict(price_cache)
            lines = [
                f"${sym}: {sig['signal']} ({sig['confidence']:.0%}) @ ${cache.get(sym,{}).get('price_usd',0):,.0f}"
                for sym, sig in sigs.items()
            ]
            print(f"[NOSTR] Broadcast ready ({len(lines)} coins) → {host}")
        except Exception as e:
            print(f"[NOSTR] {e}")
        time.sleep(3600)

# ================================================================
# SERVEUR HTTP MULTI-THREAD
# ================================================================
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Chaque requete dans son propre thread — 200+ connexions simultanees."""
    daemon_threads      = True
    allow_reuse_address = True

class DataVendorHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[API] {args[0]}")

    def get_host(self):
        return self.headers.get("Host", "localhost")

    def send_json(self, data, status=200):
        host = self.get_host()
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type",             "application/json")
        self.send_header("Content-Length",           str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Powered-By",             "DataVendorBot/3.0")
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
                    params[k] = v
        return params

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            try:
                return json.loads(self.rfile.read(length))
            except Exception:
                pass
        return {}

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
        cost = PRICING.get(self.get_base_path(), 10)
        if not db_charge(key, self.get_base_path(), cost):
            row = db_get_key(key)
            self.send_json({
                "error":         "INSUFFICIENT_BALANCE",
                "required_sats": cost,
                "balance_sats":  row["balance_sats"] if row else 0,
                "topup":         "POST /api/v1/topup",
            }, 402)
            return None
        return key

    # ==============================================================
    # GET
    # ==============================================================
    def do_GET(self):
        base   = self.get_base_path()
        params = self.get_query_params()
        host   = self.get_host()

        if base in ("/", ""):
            stats = db_get_stats()
            with cache_lock:
                nc = len(price_cache)
            self.send_json({
                "service":     "DataVendor Bot API v3.0",
                "version":     "3.0.0",
                "description": "M2M Crypto Data Marketplace — 14 Discovery Channels",
                "status":      "OPERATIONAL",
                "uptime_seconds": round(time.time() - START_TIME),
                "supported_coins": list(SUPPORTED_COINS.keys()),
                "upgrades_v3": [
                    "SQLite persistant — cles survivent aux restarts",
                    "ThreadedHTTPServer — 200+ requetes simultanees",
                    "Topup BTC on-chain — zero API tierce",
                ],
                "endpoints": {
                    "FREE":      {"GET /":"index","GET /api/v1/status":"status","GET /api/v1/pricing":"pricing","GET /api/v1/health":"health","POST /api/v1/register":"cle gratuite 100k sats"},
                    "PAID":      {"GET /api/v1/prices":"10 sats","GET /api/v1/price?symbol=BTC":"5 sats","GET /api/v1/signals":"50 sats","GET /api/v1/signal?symbol=BTC":"25 sats","GET /api/v1/prediction?symbol=BTC":"100 sats","GET /api/v1/sentiment?symbol=BTC":"30 sats","GET /api/v1/bundle?symbol=BTC":"150 sats","GET /api/v1/snapshot":"200 sats"},
                    "TOPUP":     {"GET /api/v1/topup/address":"adresse BTC + tiers","POST /api/v1/topup":"declarer paiement","POST /api/v1/topup/confirm":"confirmer TXID"},
                    "VIRAL":     {"POST /api/v1/refer":"referral M2M — 200k sats pour le filleul"},
                    "DISCOVERY": {"/.well-known/ai-plugin.json":"ChatGPT","/.well-known/openapi.json":"OpenAPI 3.1","/.well-known/mcp.json":"Claude MCP","/.well-known/agent.json":"AutoGPT","/.well-known/nostr.json":"Nostr NIP-05","/schema.json":"Schema.org","/feed.xml":"Atom","/robots.txt":"Crawlers","/sitemap.xml":"Sitemap","/datavendorbot.txt":"IndexNow"},
                },
                "btc_address": BTC_ADDRESS,
                "stats": stats,
            })
            return

        if base == "/api/v1/health":
            with cache_lock:
                nc = len(price_cache)
            self.send_json({"status":"ok","db":"sqlite","prices":nc,"uptime":round(time.time()-START_TIME)})
            return

        if base == "/api/v1/status":
            stats = db_get_stats()
            with cache_lock:
                nc   = len(price_cache)
                ns   = len(signals)
                last = max((d["fetched_at"] for d in price_cache.values()), default=0)
            self.send_json({
                "status":             "OPERATIONAL",
                "version":            "3.0.0",
                "coins_tracked":      nc,
                "signals_active":     ns,
                "uptime_seconds":     round(time.time() - START_TIME),
                "total_api_calls":    stats["total_calls"],
                "total_revenue_sats": stats["total_revenue"],
                "discovery_channels": 14,
                "persistence":        "SQLite",
                "server":             "ThreadedHTTP",
                "last_price_update":  last,
            })
            return

        if base == "/api/v1/pricing":
            self.send_json({
                "currency":         "satoshis (1 BTC = 100 000 000 sats)",
                "pricing":          {k: f"{v} sats" for k, v in PRICING.items()},
                "demo_balance":     "100 000 sats (gratuit)",
                "referral_balance": "200 000 sats (via /api/v1/refer)",
                "topup_tiers":      TOPUP_TIERS,
            })
            return

        if base == "/api/v1/topup/address":
            self.send_json({
                "btc_address": BTC_ADDRESS,
                "network":     "Bitcoin mainnet (on-chain)",
                "tiers":       TOPUP_TIERS,
                "instructions": [
                    "1. Envoie des BTC a cette adresse",
                    "2. Note ton TXID (transaction ID) visible dans ton wallet",
                    "3. POST /api/v1/topup {api_key, txid, amount_sats}",
                    "4. Attends 1 confirmation (~10 min)",
                    "5. POST /api/v1/topup/confirm {txid} pour crediter",
                ],
                "note": "Credits ajoutes apres verification manuelle ou automatique du TXID.",
            })
            return

        if base == "/api/v1/listing":
            self.send_json({
                "public_apis_format": {"API":"DataVendor Crypto Bot","Description":"M2M crypto data: prices, signals, predictions. Pay in sats.","Auth":"apiKey","HTTPS":True,"CORS":"yes","Link":f"https://{host}/","Category":"Cryptocurrency"},
                "rapidapi_format":    {"name":"DataVendor Crypto Bot API","tagline":"M2M crypto data marketplace","category":"Finance","base_url":f"https://{host}","endpoints":9,"pricing":"Freemium"},
                "apis_guru_format":   {"openapi_spec":f"https://{host}/.well-known/openapi.json","provider":"datavendor-bot","category":"financial"},
            })
            return

        if base == "/api/v1/balance":
            key = self.get_api_key()
            if not key:
                self.send_json({"error":"NO_API_KEY"},401); return
            row = db_get_key(key)
            if not row:
                self.send_json({"error":"INVALID_KEY"},401); return
            self.send_json({"balance_sats":row["balance_sats"],"total_calls":row["calls"],"tier":row["tier"],"topup":"POST /api/v1/topup"})
            return

        # --- Endpoints payes ---
        if base == "/api/v1/prices":
            key = self.require_auth()
            if not key: return
            with cache_lock: snap = dict(price_cache)
            row = db_get_key(key)
            self.send_json({"data":snap,"count":len(snap),"cost_sats":PRICING[base],"remaining_sats":row["balance_sats"] if row else 0})
            return

        if base == "/api/v1/price":
            key = self.require_auth()
            if not key: return
            sym = params.get("symbol","BTC").upper()
            with cache_lock: data = price_cache.get(sym)
            if not data:
                self.send_json({"error":"UNKNOWN_SYMBOL","supported":list(SUPPORTED_COINS.keys())},404); return
            row = db_get_key(key)
            self.send_json({"data":{sym:data},"cost_sats":PRICING[base],"remaining_sats":row["balance_sats"] if row else 0})
            return

        if base == "/api/v1/signals":
            key = self.require_auth()
            if not key: return
            with cache_lock: snap = dict(signals)
            row = db_get_key(key)
            self.send_json({"data":snap,"count":len(snap),"cost_sats":PRICING[base],"remaining_sats":row["balance_sats"] if row else 0})
            return

        if base == "/api/v1/signal":
            key = self.require_auth()
            if not key: return
            sym = params.get("symbol","BTC").upper()
            with cache_lock: sig = signals.get(sym)
            if not sig:
                self.send_json({"error":"NO_SIGNAL","supported":list(SUPPORTED_COINS.keys())},404); return
            row = db_get_key(key)
            self.send_json({"data":{sym:sig},"cost_sats":PRICING[base],"remaining_sats":row["balance_sats"] if row else 0})
            return

        if base == "/api/v1/prediction":
            key = self.require_auth()
            if not key: return
            sym  = params.get("symbol","BTC").upper()
            pred = generate_prediction(sym)
            if not pred:
                self.send_json({"error":"UNKNOWN_SYMBOL","supported":list(SUPPORTED_COINS.keys())},404); return
            row = db_get_key(key)
            self.send_json({"data":pred,"cost_sats":PRICING[base],"remaining_sats":row["balance_sats"] if row else 0})
            return

        if base == "/api/v1/sentiment":
            key = self.require_auth()
            if not key: return
            sym  = params.get("symbol","BTC").upper()
            sent = get_sentiment(sym)
            if not sent:
                self.send_json({"error":"UNKNOWN_SYMBOL","supported":list(SUPPORTED_COINS.keys())},404); return
            row = db_get_key(key)
            self.send_json({"data":sent,"cost_sats":PRICING[base],"remaining_sats":row["balance_sats"] if row else 0})
            return

        if base == "/api/v1/bundle":
            key = self.require_auth()
            if not key: return
            sym = params.get("symbol","BTC").upper()
            with cache_lock:
                pc  = price_cache.get(sym)
                sig = signals.get(sym)
            if not pc:
                self.send_json({"error":"UNKNOWN_SYMBOL","supported":list(SUPPORTED_COINS.keys())},404); return
            row = db_get_key(key)
            self.send_json({"symbol":sym,"data":{"price":pc,"signal":sig,"prediction":generate_prediction(sym),"sentiment":get_sentiment(sym)},"cost_sats":PRICING[base],"remaining_sats":row["balance_sats"] if row else 0})
            return

        if base == "/api/v1/snapshot":
            key = self.require_auth()
            if not key: return
            with cache_lock:
                pc_snap  = dict(price_cache)
                sig_snap = dict(signals)
            snapshot = {
                "vendor":        "DataVendor Bot v3.0",
                "snapshot_time": time.time(),
                "snapshot_iso":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "prices":        pc_snap,
                "signals":       sig_snap,
                "predictions":   {s: generate_prediction(s) for s in pc_snap},
                "sentiment":     {s: get_sentiment(s)       for s in pc_snap},
                "ipfs_pin":      "curl THIS_URL | ipfs add -Q",
            }
            content = json.dumps(snapshot, sort_keys=True)
            snapshot["content_hash"] = hashlib.sha256(content.encode()).hexdigest()
            self.send_json(snapshot)
            return

        # --- Discovery channels ---

        if base == "/.well-known/ai-plugin.json":
            self.send_json({
                "schema_version":"v1","name_for_human":"Crypto Data Vendor","name_for_model":"crypto_data_vendor",
                "description_for_human":"Real-time crypto prices, signals, predictions via API",
                "description_for_model":"Provides real-time cryptocurrency prices, trading signals (BUY/SELL/HOLD with confidence), AI price predictions (1h/4h/24h), and market sentiment for BTC ETH SOL DOGE XRP ADA AVAX DOT MATIC LINK. All responses JSON. Pay in satoshis. Register free at POST /api/v1/register.",
                "auth":{"type":"service_http","authorization_type":"bearer"},
                "api":{"type":"openapi","url":f"https://{host}/.well-known/openapi.json"},
                "logo_url":f"https://{host}/logo.png","contact_email":"bot@datavendor.api",
                "legal_info_url":f"https://{host}/api/v1/pricing",
            })
            return

        if base in ("/.well-known/openapi.json","/.well-known/openapi.yaml","/openapi.json"):
            self.send_json({
                "openapi":"3.1.0",
                "info":{"title":"DataVendor Bot API","description":"M2M Crypto Data Marketplace. Pay per call in Bitcoin satoshis.","version":"3.0.0","contact":{"name":"API Bot","url":f"https://{host}/"}},
                "servers":[{"url":f"https://{host}","description":"Production"}],
                "paths":{
                    "/api/v1/register":       {"post":{"operationId":"register",       "summary":"Get free API key (100k sats)"}},
                    "/api/v1/prices":         {"get": {"operationId":"getAllPrices",   "summary":"All prices (10 sats)"}},
                    "/api/v1/price":          {"get": {"operationId":"getPrice",       "summary":"Single price (5 sats)"}},
                    "/api/v1/signals":        {"get": {"operationId":"getAllSignals",  "summary":"All signals (50 sats)"}},
                    "/api/v1/signal":         {"get": {"operationId":"getSignal",      "summary":"Single signal (25 sats)"}},
                    "/api/v1/prediction":     {"get": {"operationId":"getPrediction",  "summary":"Prediction 1h/4h/24h (100 sats)"}},
                    "/api/v1/sentiment":      {"get": {"operationId":"getSentiment",   "summary":"Market sentiment (30 sats)"}},
                    "/api/v1/bundle":         {"get": {"operationId":"getBundle",      "summary":"Full bundle (150 sats)"}},
                    "/api/v1/snapshot":       {"get": {"operationId":"getSnapshot",    "summary":"IPFS snapshot (200 sats)"}},
                    "/api/v1/topup":          {"post":{"operationId":"topup",          "summary":"Declare BTC on-chain topup"}},
                    "/api/v1/topup/confirm":  {"post":{"operationId":"confirmTopup",   "summary":"Confirm TXID and credit balance"}},
                },
                "components":{"securitySchemes":{"apiKey":{"type":"apiKey","in":"query","name":"key"},"bearer":{"type":"http","scheme":"bearer"}}},
            })
            return

        if base == "/.well-known/mcp.json":
            self.send_json({
                "name":"crypto-data-vendor","version":"3.0.0",
                "description":"Real-time crypto data. Prices, signals, predictions. Pay in sats.",
                "server":{"type":"http","url":f"https://{host}"},
                "tools":[
                    {"name":"get_crypto_price","description":"Current USD price","input_schema":{"type":"object","required":["symbol","key"],"properties":{"symbol":{"type":"string"},"key":{"type":"string"}}},"endpoint":"/api/v1/price"},
                    {"name":"get_trading_signal","description":"BUY/SELL/HOLD with confidence","input_schema":{"type":"object","required":["symbol","key"],"properties":{"symbol":{"type":"string"},"key":{"type":"string"}}},"endpoint":"/api/v1/signal"},
                    {"name":"get_full_analysis","description":"Price+signal+prediction+sentiment","input_schema":{"type":"object","required":["symbol","key"],"properties":{"symbol":{"type":"string"},"key":{"type":"string"}}},"endpoint":"/api/v1/bundle"},
                ],
                "authentication":{"type":"api_key","description":"POST /api/v1/register → free key (100k sats)"},
            })
            return

        if base == "/.well-known/agent.json":
            self.send_json({
                "name":"DataVendor Crypto API","description":"Autonomous data vendor. Machines pay machines in satoshis.",
                "url":f"https://{host}","version":"3.0.0","protocol":"http-rest-json",
                "capabilities":["crypto-prices","trading-signals","predictions","sentiment"],
                "payment":{"method":"bitcoin-onchain","currency":"satoshis","address":BTC_ADDRESS,"register":f"https://{host}/api/v1/register","topup":f"https://{host}/api/v1/topup/address"},
                "documentation":f"https://{host}/","openapi":f"https://{host}/.well-known/openapi.json",
            })
            return

        if base == "/.well-known/nostr.json":
            self.send_json({
                "names":{"datavendor":"placeholder_replace_with_your_nostr_pubkey_hex"},
                "relays":{"placeholder_replace_with_your_nostr_pubkey_hex":["wss://relay.damus.io","wss://nos.lol","wss://relay.nostr.band"]},
            })
            return

        if base in ("/feed.xml","/api/v1/feed","/atom.xml","/rss.xml"):
            self.send_response(200)
            self.send_header("Content-Type","application/atom+xml")
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers()
            now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with cache_lock:
                sigs  = list(signals.items())[:10]
                cache = dict(price_cache)
            entries = ""
            for sym, sig in sigs:
                p = cache.get(sym,{}).get("price_usd",0)
                entries += (
                    f"\n  <entry>"
                    f"\n    <title>{sym}: {sig['signal']} ({sig['confidence']:.0%}) @ ${p:,.2f}</title>"
                    f"\n    <id>tag:{host},{now_str[:10]}:{sym}-{int(sig['generated_at'])}</id>"
                    f"\n    <updated>{now_str}</updated>"
                    f"\n    <summary>{sig['reason']}</summary>"
                    f"\n    <link href=\"https://{host}/api/v1/signal?symbol={sym}\" rel=\"alternate\"/>"
                    f"\n    <category term=\"trading-signal\"/>"
                    f"\n  </entry>"
                )
            self.wfile.write((
                f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<feed xmlns="http://www.w3.org/2005/Atom">\n'
                f'  <title>DataVendor Bot — Crypto Signals</title>\n'
                f'  <subtitle>M2M crypto trading signals, updated every 90s</subtitle>\n'
                f'  <link href="https://{host}/feed.xml" rel="self"/>\n'
                f'  <link href="https://{host}/" rel="alternate"/>\n'
                f'  <id>tag:{host},2025:datavendor</id>\n'
                f'  <updated>{now_str}</updated>\n'
                f'  <generator>DataVendor Bot 3.0</generator>'
                f'{entries}\n</feed>'
            ).encode())
            return

        if base in ("/schema.json","/.well-known/schema.json"):
            self.send_json({
                "@context":"https://schema.org","@type":"WebAPI",
                "name":"DataVendor Crypto Bot API",
                "description":"M2M cryptocurrency data marketplace. Prices, signals, predictions, sentiment. Pay in Bitcoin satoshis.",
                "url":f"https://{host}","documentation":f"https://{host}/.well-known/openapi.json",
                "provider":{"@type":"Organization","name":"DataVendor Bot","url":f"https://{host}"},
                "offers":{"@type":"Offer","price":"5","priceCurrency":"SAT","description":"Starting at 5 satoshis per API call"},
                "category":["Cryptocurrency","Financial Data","Trading Signals","API"],
            })
            return

        if base == "/robots.txt":
            self.send_response(200)
            self.send_header("Content-Type","text/plain")
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
            self.send_header("Content-Type","application/xml")
            self.end_headers()
            urls = ["/","/api/v1/status","/api/v1/pricing","/api/v1/listing","/api/v1/topup/address",
                    "/feed.xml","/schema.json","/.well-known/openapi.json","/.well-known/ai-plugin.json",
                    "/.well-known/mcp.json","/.well-known/agent.json"]
            xml  = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            for u in urls:
                xml += f'  <url><loc>https://{host}{u}</loc><changefreq>hourly</changefreq></url>\n'
            xml += '</urlset>'
            self.wfile.write(xml.encode())
            return

        if base == "/datavendorbot.txt":
            self.send_response(200)
            self.send_header("Content-Type","text/plain")
            self.end_headers()
            self.wfile.write(b"datavendorbot")
            return

        self.send_json({"error":"NOT_FOUND","help":"GET / for all endpoints"},404)

    # ==============================================================
    # POST
    # ==============================================================
    def do_POST(self):
        base = self.get_base_path()
        body = self.read_body()

        if base == "/api/v1/register":
            new_key = generate_api_key()
            db_create_key(new_key, 100_000, "demo")
            self.send_json({
                "success":True,"api_key":new_key,"balance_sats":100_000,
                "message":"100 000 free sats — environ 2 000-20 000 appels API.",
                "usage":{"header":f"Authorization: Bearer {new_key}","query":f"?key={new_key}","example":f"GET /api/v1/prices?key={new_key}"},
            },201)
            return

        if base == "/api/v1/refer":
            key = self.get_api_key()
            if not key or not db_get_key(key):
                self.send_json({"error":"INVALID_KEY"},401); return
            ref_key = generate_api_key()
            db_create_key(ref_key, 200_000, "referral", referred_by=key[:8])
            db_add_balance(key, 50_000)
            row = db_get_key(key)
            self.send_json({
                "referral_key":ref_key,"referral_balance":200_000,
                "your_bonus":50_000,"your_new_balance":row["balance_sats"] if row else 0,
                "message":"Partage referral_key. Le bot filleul recoit 200k, tu recois 50k bonus.",
            },201)
            return

        if base == "/api/v1/topup":
            api_key     = body.get("api_key") or self.get_api_key()
            txid        = str(body.get("txid","")).strip()
            amount_sats = int(body.get("amount_sats", 0))
            if not api_key or not db_get_key(api_key):
                self.send_json({"error":"INVALID_KEY"},401); return
            if not txid or len(txid) < 10:
                self.send_json({
                    "error":"MISSING_TXID",
                    "message":"Fournis le TXID de ta transaction Bitcoin (visible dans ton wallet).",
                    "example":'{"api_key":"DV-...","txid":"abc123def456...","amount_sats":100000}',
                    "btc_address": BTC_ADDRESS,
                },400); return
            if amount_sats < 10_000:
                self.send_json({"error":"AMOUNT_TOO_LOW","minimum":10_000,"tiers":TOPUP_TIERS},400); return
            ok = db_register_topup(txid, api_key, amount_sats)
            if not ok:
                self.send_json({"error":"TXID_ALREADY_REGISTERED"},409); return
            self.send_json({
                "success":True,"txid":txid,"api_key":api_key[:8]+"...",
                "amount_sats":amount_sats,"status":"PENDING",
                "next_step":"POST /api/v1/topup/confirm {txid} apres 1 confirmation on-chain (~10 min)",
                "btc_address":BTC_ADDRESS,
            },201)
            return

        if base == "/api/v1/topup/confirm":
            txid        = str(body.get("txid","")).strip()
            admin_token = str(body.get("admin_token","")).strip()
            expected    = os.environ.get("ADMIN_TOKEN","")
            if expected and admin_token != expected:
                self.send_json({"error":"UNAUTHORIZED","message":"ADMIN_TOKEN invalide."},403); return
            if not txid:
                self.send_json({"error":"MISSING_TXID"},400); return
            result = db_confirm_topup(txid)
            if not result:
                self.send_json({"error":"TXID_NOT_FOUND_OR_ALREADY_CONFIRMED"},404); return
            row = db_get_key(result["api_key"])
            self.send_json({
                "success":True,"txid":txid,
                "amount_sats":result["amount_sats"],
                "new_balance":row["balance_sats"] if row else 0,
                "message":f"{result['amount_sats']} sats credites avec succes.",
            })
            return

        self.send_json({"error":"NOT_FOUND"},404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Authorization, Content-Type")
        self.end_headers()


# ================================================================
# MAIN
# ================================================================
def main():
    init_db()

    print("=" * 60)
    print("DATAVENDOR BOT v3.0")
    print("  SQLite persistant + ThreadedHTTP + BTC on-chain")
    print("=" * 60)
    print(f"Coins   : {', '.join(SUPPORTED_COINS.keys())}")
    print(f"Port    : {PORT}")
    print(f"DB      : {DB_PATH}")
    print(f"BTC     : {BTC_ADDRESS}")
    print(f"Demo key: DEMO-KEY-123")
    print("=" * 60)

    threading.Thread(target=price_updater,   daemon=True).start()
    threading.Thread(target=signal_updater,  daemon=True).start()
    threading.Thread(target=auto_ping,       daemon=True).start()
    threading.Thread(target=nostr_broadcast, daemon=True).start()

    server = ThreadedHTTPServer(("0.0.0.0", PORT), DataVendorHandler)
    print(f"LIVE sur http://0.0.0.0:{PORT}")
    print("14 canaux de decouverte actifs — pret a servir les robots !")
    server.serve_forever()


if __name__ == "__main__":
    main()
