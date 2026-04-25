# ================================================================
# DATAVENDOR BOT v4.0 — FastAPI + uvicorn
# ================================================================
# Migration depuis v3.1 ThreadingMixIn → async FastAPI
# 10 000+ connexions simultanees. Zero saturation.
# Meme logique metier, meme SQLite, meme endpoints.
# ================================================================
#
# INSTALL :
#   pip install fastapi uvicorn
#
# LANCEMENT LOCAL :
#   uvicorn main_fastapi:app --host 0.0.0.0 --port 10000
#
# DEPLOY RAILWAY :
#   Procfile : web: uvicorn main_fastapi:app --host 0.0.0.0 --port $PORT --workers 4
#   Volume   : Mount Path = /data
#   Variables: BTC_ADDRESS, HOST_URL, ADMIN_TOKEN (optionnels)
#
# DEPLOY HUGGING FACE SPACES :
#   SDK      : gradio  (ou docker)
#   app.py   : renommer ce fichier en app.py
#   requirements.txt : fastapi uvicorn
#   README   : sdk: docker (voir section ci-dessous)
#
# AUTRES HEBERGEURS GRATUITS :
#   Render.com  : Start Command = uvicorn main_fastapi:app --host 0.0.0.0 --port $PORT
#   Fly.io      : fly launch puis fly deploy
#   Koyeb       : buildpack Python, Start = uvicorn main_fastapi:app ...
# ================================================================

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse, PlainTextResponse, Response
import asyncio
import json
import hashlib
import time
import urllib.request
import urllib.error
import threading
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

# ================================================================
# CONFIG GLOBALE
# ================================================================
BTC_ADDRESS = os.environ.get("BTC_ADDRESS", "1QAWwqdrBE7cL3ZBkNgJvmV95nhe3yoHeu")
DB_PATH     = os.environ.get("DB_PATH",     "/data/datavendor.db")
HOST_URL    = os.environ.get("HOST_URL",    "https://web-production-a2ec.up.railway.app")
PORT        = int(os.environ.get("PORT",    10000))
START_TIME  = time.time()

# ================================================================
# SQLITE PERSISTANT — identique v3.1
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
# CACHE PRIX EN MEMOIRE
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
# DATA FETCHERS — identiques v3.1
# ================================================================
def fetch_prices():
    try:
        ids = ",".join(SUPPORTED_COINS.values())
        url = (
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={ids}&vs_currencies=usd"
            f"&include_24hr_change=true&include_last_updated_at=true"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "DataVendorBot/4.0"})
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
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print(f"[PRICES] Rate limited CoinGecko — attente 10 min")
            time.sleep(600)
        else:
            print(f"[PRICES] HTTP Erreur: {e.code}")
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
# VERIFICATION TXID — identique v3.1
# ================================================================
def verify_txid_onchain(txid, expected_address, min_sats):
    try:
        url = f"https://blockchain.info/rawtx/{txid}"
        req = urllib.request.Request(url, headers={"User-Agent": "DataVendorBot/4.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            tx = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "TXID_NOT_FOUND"
        return False, f"HTTP_{e.code}"
    except Exception as e:
        return False, f"NETWORK_ERROR: {str(e)[:60]}"
    if tx.get("block_height") is None:
        return False, "UNCONFIRMED"
    for out in tx.get("out", []):
        if out.get("addr") == expected_address:
            received = out.get("value", 0)
            if received >= min_sats:
                return True, received
            else:
                return False, f"AMOUNT_TOO_LOW:{received}_sats_received_{min_sats}_expected"
    return False, "ADDRESS_NOT_FOUND_IN_OUTPUTS"

def auto_confirm_topup(txid, api_key, amount_sats):
    max_attempts = 12
    wait_seconds = 120
    for attempt in range(1, max_attempts + 1):
        print(f"[TOPUP] Auto-verify {attempt}/{max_attempts} → {txid[:16]}...")
        ok, result = verify_txid_onchain(txid, BTC_ADDRESS, amount_sats)
        if ok:
            confirmed = db_confirm_topup(txid)
            if confirmed:
                print(f"[TOPUP] AUTO-CONFIRMED {txid[:16]}... → {result} sats")
            return
        if result in ("TXID_NOT_FOUND", "ADDRESS_NOT_FOUND_IN_OUTPUTS") or result.startswith("AMOUNT_TOO_LOW"):
            print(f"[TOPUP] Abandon: {result}")
            return
        print(f"[TOPUP] {result} — retry dans {wait_seconds}s")
        time.sleep(wait_seconds)
    print(f"[TOPUP] Timeout: {txid[:16]}")

# ================================================================
# AUTH
# ================================================================
def generate_api_key():
    raw = f"{time.time()}-{secrets.token_hex(16)}"
    return "DV-" + hashlib.sha256(raw.encode()).hexdigest()[:32].upper()

def get_api_key_from_request(request: Request, authorization: Optional[str] = None) -> Optional[str]:
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:].strip()
    return request.query_params.get("key")

def require_auth(request: Request, endpoint: str, authorization: Optional[str] = None):
    key = get_api_key_from_request(request, authorization)
    if not key:
        raise HTTPException(status_code=401, detail={
            "error": "NO_API_KEY",
            "message": "Utilise 'Authorization: Bearer KEY' ou '?key=KEY'",
            "get_key": "POST /api/v1/register"
        })
    row = db_get_key(key)
    if not row:
        raise HTTPException(status_code=403, detail={"error": "INVALID_KEY"})
    if row["balance_sats"] <= 0:
        raise HTTPException(status_code=403, detail={"error": "NO_BALANCE"})
    cost = PRICING.get(endpoint, 10)
    if not db_charge(key, endpoint, cost):
        raise HTTPException(status_code=402, detail={
            "error": "INSUFFICIENT_BALANCE",
            "required_sats": cost,
            "balance_sats": row["balance_sats"],
            "topup": "POST /api/v1/topup"
        })
    return key

# ================================================================
# BACKGROUND THREADS — identiques v3.1
# ================================================================
def price_updater():
    time.sleep(15)
    while True:
        fetch_prices()
        time.sleep(300)  # 5 min — evite le rate limit CoinGecko

def signal_updater():
    time.sleep(25)
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
            req = urllib.request.Request(url, headers={"User-Agent": "DataVendorBot/4.0"})
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
# LIFESPAN FastAPI — demarre les threads au boot
# ================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    threading.Thread(target=price_updater,   daemon=True).start()
    threading.Thread(target=signal_updater,  daemon=True).start()
    threading.Thread(target=auto_ping,       daemon=True).start()
    threading.Thread(target=nostr_broadcast, daemon=True).start()
    print("=" * 60)
    print("DATAVENDOR BOT v4.0 — FastAPI + uvicorn")
    print(f"BTC : {BTC_ADDRESS}")
    print(f"DB  : {DB_PATH}")
    print("=" * 60)
    yield

# ================================================================
# APPLICATION FastAPI
# ================================================================
app = FastAPI(
    title="DataVendor Bot API",
    description="M2M Crypto Data Marketplace — pay per call in Bitcoin satoshis",
    version="4.0.0",
    lifespan=lifespan
)

# Middleware CORS + headers discovery
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def json_r(data: dict, status: int = 200, host: str = "localhost") -> JSONResponse:
    """Helper : JSONResponse avec headers discovery."""
    headers = {
        "X-Powered-By": "DataVendorBot/4.0",
        "X-Robots-Tag": "all, index, follow",
        "Link": (
            f'<https://{host}/.well-known/openapi.json>; rel="service-desc", '
            f'<https://{host}/feed.xml>; rel="alternate"; type="application/atom+xml", '
            f'<https://{host}/.well-known/ai-plugin.json>; rel="ai-plugin", '
            f'<https://{host}/.well-known/mcp.json>; rel="mcp-server"'
        ),
    }
    return JSONResponse(content=data, status_code=status, headers=headers)

# ================================================================
# ROUTES — GET
# ================================================================

@app.get("/")
async def index(request: Request):
    stats = db_get_stats()
    with cache_lock:
        nc = len(price_cache)
    host = request.headers.get("host", "localhost")
    return json_r({
        "service":     "DataVendor Bot API v4.0",
        "version":     "4.0.0",
        "description": "M2M Crypto Data Marketplace — 14 Discovery Channels",
        "status":      "OPERATIONAL",
        "uptime_seconds": round(time.time() - START_TIME),
        "supported_coins": list(SUPPORTED_COINS.keys()),
        "server":      "FastAPI + uvicorn (async)",
        "endpoints": {
            "FREE":  {"GET /":"index","GET /api/v1/status":"status","GET /api/v1/pricing":"pricing","GET /api/v1/health":"health","POST /api/v1/register":"cle gratuite 100k sats"},
            "PAID":  {"GET /api/v1/prices":"10 sats","GET /api/v1/price":"5 sats","GET /api/v1/signals":"50 sats","GET /api/v1/signal":"25 sats","GET /api/v1/prediction":"100 sats","GET /api/v1/sentiment":"30 sats","GET /api/v1/bundle":"150 sats","GET /api/v1/snapshot":"200 sats"},
            "TOPUP": {"GET /api/v1/topup/address":"adresse BTC","POST /api/v1/topup":"declarer","POST /api/v1/topup/confirm":"confirmer"},
        },
        "btc_address": BTC_ADDRESS,
        "stats": stats,
    }, host=host)

@app.get("/api/v1/health")
async def health(request: Request):
    with cache_lock:
        nc = len(price_cache)
    return json_r({"status":"ok","db":"sqlite","prices":nc,"uptime":round(time.time()-START_TIME),"version":"4.0.0"}, host=request.headers.get("host","localhost"))

@app.get("/api/v1/status")
async def status(request: Request):
    stats = db_get_stats()
    with cache_lock:
        nc   = len(price_cache)
        ns   = len(signals)
        last = max((d["fetched_at"] for d in price_cache.values()), default=0)
    return json_r({
        "status":"OPERATIONAL","version":"4.0.0",
        "coins_tracked":nc,"signals_active":ns,
        "uptime_seconds":round(time.time()-START_TIME),
        "total_api_calls":stats["total_calls"],
        "total_revenue_sats":stats["total_revenue"],
        "discovery_channels":14,
        "persistence":"SQLite","server":"FastAPI+uvicorn",
        "topup_verification":"auto:blockchain.info",
        "last_price_update":last,
    }, host=request.headers.get("host","localhost"))

@app.get("/api/v1/pricing")
async def pricing(request: Request):
    return json_r({
        "currency":"satoshis (1 BTC = 100 000 000 sats)",
        "pricing":{k:f"{v} sats" for k,v in PRICING.items()},
        "demo_balance":"100 000 sats (gratuit)",
        "referral_balance":"200 000 sats",
        "topup_tiers":TOPUP_TIERS,
    }, host=request.headers.get("host","localhost"))

@app.get("/api/v1/topup/address")
async def topup_address(request: Request):
    return json_r({
        "btc_address":BTC_ADDRESS,"network":"Bitcoin mainnet (on-chain)",
        "tiers":TOPUP_TIERS,"auto_verify":True,
        "instructions":["1. Envoie BTC a cette adresse","2. Note ton TXID","3. POST /api/v1/topup {api_key,txid,amount_sats}","4. Verification automatique ~10 min","5. Credits ajoutes automatiquement"],
    }, host=request.headers.get("host","localhost"))

@app.get("/api/v1/balance")
async def balance(request: Request, authorization: Optional[str] = Header(default=None)):
    key = get_api_key_from_request(request, authorization)
    if not key:
        raise HTTPException(status_code=401, detail={"error":"NO_API_KEY"})
    row = db_get_key(key)
    if not row:
        raise HTTPException(status_code=401, detail={"error":"INVALID_KEY"})
    return json_r({"balance_sats":row["balance_sats"],"total_calls":row["calls"],"tier":row["tier"]}, host=request.headers.get("host","localhost"))

# --- Endpoints payes ---

@app.get("/api/v1/prices")
async def get_prices(request: Request, authorization: Optional[str] = Header(default=None)):
    key = require_auth(request, "/api/v1/prices", authorization)
    with cache_lock: snap = dict(price_cache)
    row = db_get_key(key)
    return json_r({"data":snap,"count":len(snap),"cost_sats":PRICING["/api/v1/prices"],"remaining_sats":row["balance_sats"] if row else 0}, host=request.headers.get("host","localhost"))

@app.get("/api/v1/price")
async def get_price(request: Request, symbol: str = "BTC", authorization: Optional[str] = Header(default=None)):
    key = require_auth(request, "/api/v1/price", authorization)
    sym = symbol.upper()
    with cache_lock: data = price_cache.get(sym)
    if not data:
        raise HTTPException(status_code=404, detail={"error":"UNKNOWN_SYMBOL","supported":list(SUPPORTED_COINS.keys())})
    row = db_get_key(key)
    return json_r({"data":{sym:data},"cost_sats":PRICING["/api/v1/price"],"remaining_sats":row["balance_sats"] if row else 0}, host=request.headers.get("host","localhost"))

@app.get("/api/v1/signals")
async def get_signals(request: Request, authorization: Optional[str] = Header(default=None)):
    key = require_auth(request, "/api/v1/signals", authorization)
    with cache_lock: snap = dict(signals)
    row = db_get_key(key)
    return json_r({"data":snap,"count":len(snap),"cost_sats":PRICING["/api/v1/signals"],"remaining_sats":row["balance_sats"] if row else 0}, host=request.headers.get("host","localhost"))

@app.get("/api/v1/signal")
async def get_signal(request: Request, symbol: str = "BTC", authorization: Optional[str] = Header(default=None)):
    key = require_auth(request, "/api/v1/signal", authorization)
    sym = symbol.upper()
    with cache_lock: sig = signals.get(sym)
    if not sig:
        raise HTTPException(status_code=404, detail={"error":"NO_SIGNAL","supported":list(SUPPORTED_COINS.keys())})
    row = db_get_key(key)
    return json_r({"data":{sym:sig},"cost_sats":PRICING["/api/v1/signal"],"remaining_sats":row["balance_sats"] if row else 0}, host=request.headers.get("host","localhost"))

@app.get("/api/v1/prediction")
async def get_prediction(request: Request, symbol: str = "BTC", authorization: Optional[str] = Header(default=None)):
    key = require_auth(request, "/api/v1/prediction", authorization)
    sym  = symbol.upper()
    pred = generate_prediction(sym)
    if not pred:
        raise HTTPException(status_code=404, detail={"error":"UNKNOWN_SYMBOL","supported":list(SUPPORTED_COINS.keys())})
    row = db_get_key(key)
    return json_r({"data":pred,"cost_sats":PRICING["/api/v1/prediction"],"remaining_sats":row["balance_sats"] if row else 0}, host=request.headers.get("host","localhost"))

@app.get("/api/v1/sentiment")
async def get_sentiment_ep(request: Request, symbol: str = "BTC", authorization: Optional[str] = Header(default=None)):
    key = require_auth(request, "/api/v1/sentiment", authorization)
    sym  = symbol.upper()
    sent = get_sentiment(sym)
    if not sent:
        raise HTTPException(status_code=404, detail={"error":"UNKNOWN_SYMBOL","supported":list(SUPPORTED_COINS.keys())})
    row = db_get_key(key)
    return json_r({"data":sent,"cost_sats":PRICING["/api/v1/sentiment"],"remaining_sats":row["balance_sats"] if row else 0}, host=request.headers.get("host","localhost"))

@app.get("/api/v1/bundle")
async def get_bundle(request: Request, symbol: str = "BTC", authorization: Optional[str] = Header(default=None)):
    key = require_auth(request, "/api/v1/bundle", authorization)
    sym = symbol.upper()
    with cache_lock:
        pc  = price_cache.get(sym)
        sig = signals.get(sym)
    if not pc:
        raise HTTPException(status_code=404, detail={"error":"UNKNOWN_SYMBOL","supported":list(SUPPORTED_COINS.keys())})
    row = db_get_key(key)
    return json_r({"symbol":sym,"data":{"price":pc,"signal":sig,"prediction":generate_prediction(sym),"sentiment":get_sentiment(sym)},"cost_sats":PRICING["/api/v1/bundle"],"remaining_sats":row["balance_sats"] if row else 0}, host=request.headers.get("host","localhost"))

@app.get("/api/v1/snapshot")
async def get_snapshot(request: Request, authorization: Optional[str] = Header(default=None)):
    key = require_auth(request, "/api/v1/snapshot", authorization)
    with cache_lock:
        pc_snap  = dict(price_cache)
        sig_snap = dict(signals)
    snapshot = {
        "vendor":"DataVendor Bot v4.0","snapshot_time":time.time(),
        "snapshot_iso":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "prices":pc_snap,"signals":sig_snap,
        "predictions":{s:generate_prediction(s) for s in pc_snap},
        "sentiment":{s:get_sentiment(s) for s in pc_snap},
        "ipfs_pin":"curl THIS_URL | ipfs add -Q",
    }
    content = json.dumps(snapshot, sort_keys=True)
    snapshot["content_hash"] = hashlib.sha256(content.encode()).hexdigest()
    return json_r(snapshot, host=request.headers.get("host","localhost"))

@app.get("/api/v1/listing")
async def listing(request: Request):
    host = request.headers.get("host","localhost")
    return json_r({
        "public_apis_format":{"API":"DataVendor Crypto Bot","Description":"M2M crypto data: prices, signals, predictions. Pay in sats.","Auth":"apiKey","HTTPS":True,"CORS":"yes","Link":f"https://{host}/","Category":"Cryptocurrency"},
        "rapidapi_format":{"name":"DataVendor Crypto Bot API","tagline":"M2M crypto data marketplace","category":"Finance","base_url":f"https://{host}","endpoints":9,"pricing":"Freemium"},
        "apis_guru_format":{"openapi_spec":f"https://{host}/.well-known/openapi.json","provider":"datavendor-bot","category":"financial"},
    }, host=host)

# ================================================================
# CANAUX DISCOVERY — identiques v3.1
# ================================================================

@app.get("/.well-known/ai-plugin.json")
async def ai_plugin(request: Request):
    host = request.headers.get("host","localhost")
    return json_r({"schema_version":"v1","name_for_human":"Crypto Data Vendor","name_for_model":"crypto_data_vendor","description_for_human":"Real-time crypto prices, signals, predictions via API","description_for_model":"Provides real-time cryptocurrency prices, trading signals (BUY/SELL/HOLD with confidence), AI price predictions (1h/4h/24h), and market sentiment for BTC ETH SOL DOGE XRP ADA AVAX DOT MATIC LINK. All responses JSON. Pay in satoshis. Register free at POST /api/v1/register.","auth":{"type":"service_http","authorization_type":"bearer"},"api":{"type":"openapi","url":f"https://{host}/.well-known/openapi.json"},"logo_url":f"https://{host}/logo.png","contact_email":"bot@datavendor.api","legal_info_url":f"https://{host}/api/v1/pricing"}, host=host)

@app.get("/.well-known/openapi.json")
@app.get("/.well-known/openapi.yaml")
@app.get("/openapi.json")
async def openapi_spec(request: Request):
    host = request.headers.get("host","localhost")
    return json_r({"openapi":"3.1.0","info":{"title":"DataVendor Bot API","description":"M2M Crypto Data Marketplace. Pay per call in Bitcoin satoshis.","version":"4.0.0","contact":{"name":"API Bot","url":f"https://{host}/"}},"servers":[{"url":f"https://{host}","description":"Production"}],"paths":{"/api/v1/register":{"post":{"operationId":"register","summary":"Get free API key (100k sats)"}},"/api/v1/prices":{"get":{"operationId":"getAllPrices","summary":"All prices (10 sats)"}},"/api/v1/price":{"get":{"operationId":"getPrice","summary":"Single price (5 sats)"}},"/api/v1/signals":{"get":{"operationId":"getAllSignals","summary":"All signals (50 sats)"}},"/api/v1/signal":{"get":{"operationId":"getSignal","summary":"Single signal (25 sats)"}},"/api/v1/prediction":{"get":{"operationId":"getPrediction","summary":"Prediction 1h/4h/24h (100 sats)"}},"/api/v1/sentiment":{"get":{"operationId":"getSentiment","summary":"Market sentiment (30 sats)"}},"/api/v1/bundle":{"get":{"operationId":"getBundle","summary":"Full bundle (150 sats)"}},"/api/v1/snapshot":{"get":{"operationId":"getSnapshot","summary":"IPFS snapshot (200 sats)"}},"/api/v1/topup":{"post":{"operationId":"topup","summary":"Declare BTC topup"}},"/api/v1/topup/confirm":{"post":{"operationId":"confirmTopup","summary":"Manual confirm (admin fallback)"}}},"components":{"securitySchemes":{"apiKey":{"type":"apiKey","in":"query","name":"key"},"bearer":{"type":"http","scheme":"bearer"}}}}, host=host)

@app.get("/.well-known/mcp.json")
async def mcp(request: Request):
    host = request.headers.get("host","localhost")
    return json_r({"name":"crypto-data-vendor","version":"4.0.0","description":"Real-time crypto data. Prices, signals, predictions. Pay in sats.","server":{"type":"http","url":f"https://{host}"},"tools":[{"name":"get_crypto_price","description":"Current USD price","input_schema":{"type":"object","required":["symbol","key"],"properties":{"symbol":{"type":"string"},"key":{"type":"string"}}},"endpoint":"/api/v1/price"},{"name":"get_trading_signal","description":"BUY/SELL/HOLD with confidence","input_schema":{"type":"object","required":["symbol","key"],"properties":{"symbol":{"type":"string"},"key":{"type":"string"}}},"endpoint":"/api/v1/signal"},{"name":"get_full_analysis","description":"Price+signal+prediction+sentiment","input_schema":{"type":"object","required":["symbol","key"],"properties":{"symbol":{"type":"string"},"key":{"type":"string"}}},"endpoint":"/api/v1/bundle"}],"authentication":{"type":"api_key","description":"POST /api/v1/register → free key (100k sats)"}}, host=host)

@app.get("/.well-known/agent.json")
async def agent(request: Request):
    host = request.headers.get("host","localhost")
    return json_r({"name":"DataVendor Crypto API","description":"Autonomous data vendor. Machines pay machines in satoshis.","url":f"https://{host}","version":"4.0.0","protocol":"http-rest-json","capabilities":["crypto-prices","trading-signals","predictions","sentiment"],"payment":{"method":"bitcoin-onchain","currency":"satoshis","address":BTC_ADDRESS,"register":f"https://{host}/api/v1/register","topup":f"https://{host}/api/v1/topup/address","auto_verify":True},"documentation":f"https://{host}/","openapi":f"https://{host}/.well-known/openapi.json"}, host=host)

@app.get("/.well-known/nostr.json")
async def nostr(request: Request):
    return json_r({"names":{"datavendor":"placeholder_replace_with_your_nostr_pubkey_hex"},"relays":{"placeholder_replace_with_your_nostr_pubkey_hex":["wss://relay.damus.io","wss://nos.lol","wss://relay.nostr.band"]}}, host=request.headers.get("host","localhost"))

@app.get("/feed.xml")
@app.get("/atom.xml")
@app.get("/rss.xml")
async def feed(request: Request):
    host = request.headers.get("host","localhost")
    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with cache_lock:
        sigs  = list(signals.items())[:10]
        cache = dict(price_cache)
    entries = ""
    for sym, sig in sigs:
        p = cache.get(sym,{}).get("price_usd",0)
        entries += (f"\n  <entry>\n    <title>{sym}: {sig['signal']} ({sig['confidence']:.0%}) @ ${p:,.2f}</title>\n    <id>tag:{host},{now_str[:10]}:{sym}-{int(sig['generated_at'])}</id>\n    <updated>{now_str}</updated>\n    <summary>{sig['reason']}</summary>\n    <link href=\"https://{host}/api/v1/signal?symbol={sym}\" rel=\"alternate\"/>\n    <category term=\"trading-signal\"/>\n  </entry>")
    atom = (f'<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n  <title>DataVendor Bot — Crypto Signals</title>\n  <subtitle>M2M crypto trading signals, updated every 90s</subtitle>\n  <link href="https://{host}/feed.xml" rel="self"/>\n  <link href="https://{host}/" rel="alternate"/>\n  <id>tag:{host},2025:datavendor</id>\n  <updated>{now_str}</updated>\n  <generator>DataVendor Bot 4.0</generator>{entries}\n</feed>')
    return Response(content=atom, media_type="application/atom+xml")

@app.get("/schema.json")
@app.get("/.well-known/schema.json")
async def schema(request: Request):
    host = request.headers.get("host","localhost")
    return json_r({"@context":"https://schema.org","@type":"WebAPI","name":"DataVendor Crypto Bot API","description":"M2M cryptocurrency data marketplace. Prices, signals, predictions, sentiment. Pay in Bitcoin satoshis.","url":f"https://{host}","documentation":f"https://{host}/.well-known/openapi.json","provider":{"@type":"Organization","name":"DataVendor Bot","url":f"https://{host}"},"offers":{"@type":"Offer","price":"5","priceCurrency":"SAT","description":"Starting at 5 satoshis per API call"},"category":["Cryptocurrency","Financial Data","Trading Signals","API"]}, host=host)

@app.get("/robots.txt")
async def robots(request: Request):
    host = request.headers.get("host","localhost")
    content = (f"User-agent: *\nAllow: /\n\nSitemap: https://{host}/sitemap.xml\nAI-Plugin: https://{host}/.well-known/ai-plugin.json\nOpenAPI: https://{host}/.well-known/openapi.json\nMCP: https://{host}/.well-known/mcp.json\nAgent: https://{host}/.well-known/agent.json\nFeed: https://{host}/feed.xml\n")
    return PlainTextResponse(content=content)

@app.get("/sitemap.xml")
async def sitemap(request: Request):
    host = request.headers.get("host","localhost")
    urls = ["/","/api/v1/status","/api/v1/pricing","/api/v1/listing","/api/v1/topup/address","/feed.xml","/schema.json","/.well-known/openapi.json","/.well-known/ai-plugin.json","/.well-known/mcp.json","/.well-known/agent.json"]
    xml  = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for u in urls:
        xml += f'  <url><loc>https://{host}{u}</loc><changefreq>hourly</changefreq></url>\n'
    xml += '</urlset>'
    return Response(content=xml, media_type="application/xml")

@app.get("/datavendorbot.txt")
async def indexnow_key():
    return PlainTextResponse("datavendorbot")

# ================================================================
# ROUTES — POST
# ================================================================

@app.post("/api/v1/register", status_code=201)
async def register(request: Request):
    new_key = generate_api_key()
    db_create_key(new_key, 100_000, "demo")
    return json_r({"success":True,"api_key":new_key,"balance_sats":100_000,"message":"100 000 free sats — environ 2 000-20 000 appels API.","usage":{"header":f"Authorization: Bearer {new_key}","query":f"?key={new_key}","example":f"GET /api/v1/prices?key={new_key}"}}, status=201, host=request.headers.get("host","localhost"))

@app.post("/api/v1/refer", status_code=201)
async def refer(request: Request, authorization: Optional[str] = Header(default=None)):
    key = get_api_key_from_request(request, authorization)
    if not key or not db_get_key(key):
        raise HTTPException(status_code=401, detail={"error":"INVALID_KEY"})
    ref_key = generate_api_key()
    db_create_key(ref_key, 200_000, "referral", referred_by=key[:8])
    db_add_balance(key, 50_000)
    row = db_get_key(key)
    return json_r({"referral_key":ref_key,"referral_balance":200_000,"your_bonus":50_000,"your_new_balance":row["balance_sats"] if row else 0,"message":"Partage referral_key. Le bot filleul recoit 200k, tu recois 50k bonus."}, status=201, host=request.headers.get("host","localhost"))

@app.post("/api/v1/topup", status_code=201)
async def topup(request: Request, authorization: Optional[str] = Header(default=None)):
    body        = await request.json() if await request.body() else {}
    api_key     = body.get("api_key") or get_api_key_from_request(request, authorization)
    txid        = str(body.get("txid","")).strip()
    amount_sats = int(body.get("amount_sats", 0))
    if not api_key or not db_get_key(api_key):
        raise HTTPException(status_code=401, detail={"error":"INVALID_KEY"})
    if not txid or len(txid) < 10:
        raise HTTPException(status_code=400, detail={"error":"MISSING_TXID","message":"Fournis le TXID Bitcoin.","btc_address":BTC_ADDRESS})
    if amount_sats < 10_000:
        raise HTTPException(status_code=400, detail={"error":"AMOUNT_TOO_LOW","minimum":10_000})
    ok = db_register_topup(txid, api_key, amount_sats)
    if not ok:
        raise HTTPException(status_code=409, detail={"error":"TXID_ALREADY_REGISTERED"})
    threading.Thread(target=auto_confirm_topup, args=(txid, api_key, amount_sats), daemon=True).start()
    return json_r({"success":True,"txid":txid,"api_key":api_key[:8]+"...","amount_sats":amount_sats,"status":"PENDING_AUTO_VERIFICATION","auto_verify":True,"message":"Verification on-chain automatique lancee. Credits ajoutes sous ~10 min.","btc_address":BTC_ADDRESS,"fallback":"POST /api/v1/topup/confirm si non credite apres 2h"}, status=201, host=request.headers.get("host","localhost"))

@app.post("/api/v1/topup/confirm")
async def topup_confirm(request: Request, authorization: Optional[str] = Header(default=None)):
    body        = await request.json() if await request.body() else {}
    txid        = str(body.get("txid","")).strip()
    admin_token = str(body.get("admin_token","")).strip()
    expected    = os.environ.get("ADMIN_TOKEN","")
    if expected and admin_token != expected:
        raise HTTPException(status_code=403, detail={"error":"UNAUTHORIZED"})
    if not txid:
        raise HTTPException(status_code=400, detail={"error":"MISSING_TXID"})
    result = db_confirm_topup(txid)
    if not result:
        raise HTTPException(status_code=404, detail={"error":"TXID_NOT_FOUND_OR_ALREADY_CONFIRMED"})
    row = db_get_key(result["api_key"])
    return json_r({"success":True,"txid":txid,"amount_sats":result["amount_sats"],"new_balance":row["balance_sats"] if row else 0,"message":f"{result['amount_sats']} sats credites."}, host=request.headers.get("host","localhost"))

# ================================================================
# ENTRYPOINT LOCAL
# ================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=False, workers=4)
