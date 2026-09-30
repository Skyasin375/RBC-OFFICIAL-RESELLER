# -*- coding: utf-8 -*-
"""
RBC LEVEL UP - Dashboard Server (Render-ready + multi-device login)
"""

import asyncio
import json
import os
import time
from typing import Dict, List, Any, Optional

import httpx
from aiohttp import web

# ==================== FILE PATHS ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "index.html")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")

DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

POPUP_FILE = os.path.join(DATA_DIR, "popup.json")
TELEGRAM_FILE = os.path.join(DATA_DIR, "telegram.json")
OWNERS_FILE = os.path.join(DATA_DIR, "owners.json")
ALIASES_FILE = os.path.join(DATA_DIR, "aliases.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
CREDS_FILE = os.path.join(DATA_DIR, "credentials.json")

# Admin credentials (env override)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "skyasinali221@gmail.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "skyasin")

# Payment API
PAY_API_BASE = os.getenv("PAY_API_BASE", "https://fampaygateway.site/api")
PAY_API_KEY = os.getenv("PAY_API_KEY", "FAM_7E06068D658196F192A94D47DF9C46500389DCBB")

# CORS
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")


def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass


# ==================== BOT STATE ====================
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 300
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.owners: Dict[str, str] = {}
        self.uid_aliases: Dict[str, str] = {}

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "uid": uid
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def register_account(self, uid: str, nickname: str, region: str, level: int, exp: int, likes: int = 0):
        uid_str = str(uid)
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": level or 1,
                "initial_exp": int(exp or 0),
                "current_exp": int(exp or 0),
                "gained_exp": 0,
                "likes": likes or 0,
                "status": "ONLINE",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_updated": time.strftime("%H:%M:%S"),
                "owner": self.owners.get(uid_str, "")
            }
        else:
            acc = self.accounts[uid_str]
            if nickname: acc["nickname"] = nickname
            if region: acc["region"] = region
            if level and level > 0: acc["level"] = level
            if "initial_exp" not in acc or acc.get("initial_exp") is None:
                acc["initial_exp"] = int(exp or 0)
            elif exp and int(exp) < int(acc["initial_exp"]):
                acc["initial_exp"] = int(exp)
            prev_current = int(acc.get("current_exp") or 0)
            if int(exp or 0) > 0:
                acc["current_exp"] = int(exp)
            elif prev_current == 0:
                acc["current_exp"] = int(exp or 0)
            acc["gained_exp"] = max(0, int(acc["current_exp"]) - int(acc["initial_exp"]))
            if likes and likes > 0: acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
            acc["owner"] = self.owners.get(uid_str, acc.get("owner", ""))
        self.recalc_totals()

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str not in self.accounts: return
        acc = self.accounts[uid_str]
        old_exp = int(acc.get("current_exp") or 0)
        new_exp = int(current_exp or 0)
        if new_exp > 0 or old_exp == 0:
            acc["current_exp"] = new_exp
        if level is not None and int(level) > 0:
            acc["level"] = int(level)
        baseline = int(acc.get("initial_exp") or 0)
        if new_exp > 0 and new_exp < baseline:
            acc["initial_exp"] = new_exp
            baseline = new_exp
        acc["gained_exp"] = max(0, int(acc["current_exp"]) - baseline)
        acc["last_updated"] = time.strftime("%H:%M:%S")
        diff = int(acc["current_exp"]) - old_exp
        if diff > 0:
            self.log(
                f"Account {acc['nickname']} ({uid_str}) gained +{diff} EXP! "
                f"Total Gained: +{acc['gained_exp']}",
                "success", uid_str
            )
        self.recalc_totals()

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] = \
                int(self.accounts[uid_str].get("matches_played", 0)) + 1
            self.accounts[uid_str]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
            self.log(
                f"Account {self.accounts[uid_str]['nickname']} "
                f"finished Match #{self.accounts[uid_str]['matches_played']}",
                "info", uid_str
            )

    def recalc_totals(self):
        self.total_gained_exp = sum(int(acc.get("gained_exp", 0) or 0) for acc in self.accounts.values())

    def set_owner(self, uid: str, owner_email: str):
        uid_str = str(uid)
        self.owners[uid_str] = owner_email
        if uid_str in self.accounts:
            self.accounts[uid_str]["owner"] = owner_email

    def set_alias(self, input_uid: str, canonical_uid: str):
        if not input_uid or not canonical_uid: return
        input_str = str(input_uid)
        canon_str = str(canonical_uid)
        self.uid_aliases[input_str] = canon_str
        self.uid_aliases.setdefault(canon_str, canon_str)
        owner = self.owners.get(input_str)
        if owner:
            self.owners[canon_str] = owner
            if canon_str in self.accounts:
                self.accounts[canon_str]["owner"] = owner


bot_state = BotState()


# ==================== HELPERS ====================
def _read_json(path: str, default):
    if not os.path.exists(path): return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data) -> bool:
    try:
        _ensure_data_dir()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception as e:
        bot_state.log(f"Failed to write {path}: {e}", "error")
        return False


def _load_owners_into_state():
    data = _read_json(OWNERS_FILE, {})
    bot_state.owners = data if isinstance(data, dict) else {}


def _save_owners_from_state():
    _write_json(OWNERS_FILE, bot_state.owners)


def _load_aliases_into_state():
    data = _read_json(ALIASES_FILE, {})
    bot_state.uid_aliases = data if isinstance(data, dict) else {}


def _save_aliases_from_state():
    _write_json(ALIASES_FILE, bot_state.uid_aliases)


def _load_users():
    data = _read_json(USERS_FILE, [])
    return data if isinstance(data, list) else []


def _save_users(users):
    return _write_json(USERS_FILE, users)


def _load_creds():
    data = _read_json(CREDS_FILE, [])
    return data if isinstance(data, list) else []


def _save_creds(creds):
    return _write_json(CREDS_FILE, creds)


# ==================== CORS MIDDLEWARE ====================
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            response = ex
        except Exception as e:
            bot_state.log(f"Handler error: {e}", "error")
            response = web.json_response({"status": "error", "error": str(e)}, status=500)

    origin = request.headers.get("Origin", "")
    if ALLOWED_ORIGINS.strip() == "*":
        response.headers["Access-Control-Allow-Origin"] = "*"
    elif origin and origin in [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"

    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Max-Age"] = "86400"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ==================== BASIC HANDLERS ====================

async def handle_index(request: web.Request) -> web.Response:
    if os.path.exists(TEMPLATE_PATH):
        try:
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                content = f.read()
            return web.Response(text=content, content_type="text/html", charset="utf-8")
        except Exception as e:
            return web.Response(text=f"<h1>Error: {e}</h1>", content_type="text/html", status=500)
    return web.Response(text=f"<h1>templates/index.html not found</h1>", content_type="text/html", status=404)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "service": "rbc-level-dashboard",
        "uptime": int(time.time() - bot_state.start_time),
        "accounts": len(bot_state.accounts)
    })


async def handle_get_stats(request: web.Request) -> web.Response:
    accounts_data = list(bot_state.accounts.values())
    accounts_data.sort(key=lambda x: int(x.get("gained_exp", 0) or 0), reverse=True)
    return web.json_response({
        "status": "ok",
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "accounts": accounts_data,
        "owners": bot_state.owners,
        "aliases": bot_state.uid_aliases,
        "logs": bot_state.logs[-60:],
        "uptime": int(time.time() - bot_state.start_time)
    })


# ==================== AUTH (server-side users) ====================

async def handle_auth_login(request: web.Request) -> web.Response:
    """Authenticate user against server-side store (works from any device)."""
    try:
        data = await request.json()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        if not email or not password:
            return web.json_response({"status": "error", "error": "email & password required"})

        # Admin
        if email == ADMIN_EMAIL.lower() and password == ADMIN_PASSWORD:
            bot_state.log(f"Admin login: {email}", "success")
            return web.json_response({
                "status": "ok",
                "role": "admin",
                "email": ADMIN_EMAIL
            })

        users = _load_users()
        user = next((u for u in users if str(u.get("email", "")).lower() == email), None)
        if not user or user.get("password") != password:
            bot_state.log(f"Failed login attempt: {email}", "warning")
            return web.json_response({"status": "error", "error": "Invalid email or password"}, status=401)

        creds = _load_creds()
        cred = next((c for c in creds if str(c.get("email", "")).lower() == email), None)
        if not cred:
            return web.json_response({"status": "error", "error": "No plan found for this account"}, status=403)

        if cred.get("revoked"):
            return web.json_response({"status": "error", "error": "Credentials revoked"}, status=403)

        exp = cred.get("expiresAt") or 0
        if exp and exp > 0 and int(time.time() * 1000) > exp:
            return web.json_response({"status": "error", "error": "Plan expired"}, status=403)

        bot_state.log(f"User login: {email}", "success")
        return web.json_response({
            "status": "ok",
            "role": "user",
            "email": email,
            "user": user,
            "credential": cred
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def handle_create_user(request: web.Request) -> web.Response:
    """Create a user + credential pair on the server (works from any device)."""
    try:
        data = await request.json()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        slots = int(data.get("slots", 3))
        days = int(data.get("days", 7))
        label = str(data.get("label", "AUTO"))
        owner = str(data.get("owner", "")).strip().lower()

        if not email or not password:
            return web.json_response({"status": "error", "error": "email & password required"})

        now_ms = int(time.time() * 1000)
        expires_at = now_ms + days * 86400000 if days > 0 else 0

        users = _load_users()
        users = [u for u in users if str(u.get("email", "")).lower() != email]
        users.append({
            "name": "User",
            "email": email,
            "password": password,
            "createdAt": now_ms
        })
        _save_users(users)

        creds = _load_creds()
        creds = [c for c in creds if str(c.get("email", "")).lower() != email]
        creds.insert(0, {
            "email": email,
            "password": password,
            "slots": slots,
            "label": label,
            "revoked": False,
            "created_at": now_ms,
            "expiresAt": expires_at,
            "days": days
        })
        _save_creds(creds)

        if owner:
            bot_state.set_owner(email, owner)
            _save_owners_from_state()

        bot_state.log(f"User created: {email} ({slots} slots, {days} days)", "success")
        return web.json_response({"status": "ok", "email": email})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def handle_get_creds(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "credentials": _load_creds()})


async def handle_save_creds(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        creds = data.get("credentials", [])
        if not isinstance(creds, list):
            return web.json_response({"status": "error", "error": "credentials must be a list"})
        _save_creds(creds)
        users = [{
            "name": "User",
            "email": c.get("email", ""),
            "password": c.get("password", ""),
            "createdAt": c.get("created_at", int(time.time() * 1000))
        } for c in creds if c.get("email")]
        _save_users(users)
        bot_state.log(f"Credentials updated ({len(creds)} entries)", "success")
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


# ==================== ACCOUNT MANAGEMENT ====================

async def handle_add_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        existing = _read_json(ACCOUNTS_FILE, [])
        if not isinstance(existing, list):
            existing = []

        owner_email = str(data.get("owner", "")).strip()

        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return web.json_response({"status": "error", "error": "UID and Password are required"})
            existing = [acc for acc in existing if str(acc.get("uid")) != uid]
            entry = {"uid": uid, "password": pwd}
            if owner_email: entry["owner"] = owner_email
            existing.append(entry)
            label = uid
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token is required"})
            existing = [acc for acc in existing if acc.get("token") != token]
            entry = {"token": token}
            if owner_email: entry["owner"] = owner_email
            existing.append(entry)
            label = token[:12]
        else:
            return web.json_response({"status": "error", "error": "Invalid payload"})

        _write_json(ACCOUNTS_FILE, existing)
        bot_state.log(f"New account added: {label}", "success")

        if owner_email:
            bot_state.set_owner(label, owner_email)
            _save_owners_from_state()
            bot_state.uid_aliases.setdefault(str(label), str(label))
            _save_aliases_from_state()

        cb = bot_state.refresh_callbacks.get("on_account_added")
        if cb:
            try:
                asyncio.create_task(cb(data))
            except Exception as e:
                bot_state.log(f"on_account_added callback error: {e}", "error")

        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_delete_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})

        existing = _read_json(ACCOUNTS_FILE, [])
        if isinstance(existing, list):
            existing = [acc for acc in existing if str(acc.get("uid")) != uid]
            _write_json(ACCOUNTS_FILE, existing)

        if uid in bot_state.accounts:
            del bot_state.accounts[uid]
        if uid in bot_state.account_workers:
            try: bot_state.account_workers[uid].cancel()
            except Exception: pass
            del bot_state.account_workers[uid]
        if uid in bot_state.owners:
            del bot_state.owners[uid]
            _save_owners_from_state()

        changed = False
        for k in list(bot_state.uid_aliases.keys()):
            if k == uid or bot_state.uid_aliases[k] == uid:
                del bot_state.uid_aliases[k]
                changed = True
        if changed: _save_aliases_from_state()

        bot_state.log(f"Account {uid} removed.", "warning", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_remove_account(request: web.Request) -> web.Response:
    return await handle_delete_account(request)


async def handle_refresh_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})
        cb = bot_state.refresh_callbacks.get("on_refresh_account")
        if cb:
            try: asyncio.create_task(cb(uid))
            except Exception as e: bot_state.log(f"refresh cb error: {e}", "error")
        bot_state.log(f"Refresh requested for {uid}", "info", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_restart_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})
        if uid in bot_state.account_workers:
            try: bot_state.account_workers[uid].cancel()
            except Exception: pass
            del bot_state.account_workers[uid]
        cb = bot_state.refresh_callbacks.get("on_restart_account")
        if cb:
            try: asyncio.create_task(cb(uid))
            except Exception as e: bot_state.log(f"restart cb error: {e}", "error")
        bot_state.log(f"Restart requested for {uid}", "warning", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_stop_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})
        if uid in bot_state.accounts:
            bot_state.accounts[uid]["status"] = "PAUSED"
        if uid in bot_state.account_workers:
            try: bot_state.account_workers[uid].cancel()
            except Exception: pass
            del bot_state.account_workers[uid]
        bot_state.log(f"Bot stopped for {uid}", "warning", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_get_console(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "logs": bot_state.logs[-200:],
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "uptime": int(time.time() - bot_state.start_time)
    })


# ==================== PAYMENT PROXY ====================

async def handle_pay_create(request: web.Request) -> web.Response:
    amount = request.query.get("amount", "0")
    url = f"{PAY_API_BASE}/create_order.php?amount={amount}&api_key={PAY_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as c:
            r = await c.get(url)
        try:
            return web.json_response(r.json())
        except Exception:
            return web.json_response({"status": "error", "message": "Invalid gateway response", "raw": r.text[:300]}, status=502)
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=502)


async def handle_pay_verify(request: web.Request) -> web.Response:
    order_id = request.query.get("order_id", "")
    url = f"{PAY_API_BASE}/verify.php?order_id={order_id}&api_key={PAY_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as c:
            r = await c.get(url)
        try:
            return web.json_response(r.json())
        except Exception:
            return web.json_response({"status": "error", "message": "Invalid gateway response", "raw": r.text[:300]}, status=502)
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=502)


# ==================== POPUP / TELEGRAM / CONFIG ====================

async def handle_get_popup(request: web.Request) -> web.Response:
    default = {
        "enabled": False, "header": "Important Update", "title": "", "message": "",
        "button_text": "Chat Now", "button_link": "https://t.me/RexBullYasin",
        "icon_class": "fa-solid fa-circle-check", "icon_color": "#22c55e", "updated_at": 0
    }
    saved = _read_json(POPUP_FILE, {})
    if isinstance(saved, dict): default.update(saved)
    return web.json_response({"status": "ok", "popup": default})


async def handle_save_popup(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return web.json_response({"status": "error", "error": "Invalid payload"})
        data["updated_at"] = int(time.time())
        if not _write_json(POPUP_FILE, data):
            return web.json_response({"status": "error", "error": "Failed to save"})
        bot_state.log("Popup settings saved", "success")
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_get_telegram(request: web.Request) -> web.Response:
    default = {"link": "https://t.me/RexBullYasin", "username": "@RexBullYasin"}
    saved = _read_json(TELEGRAM_FILE, {})
    if isinstance(saved, dict): default.update(saved)
    return web.json_response({"status": "ok", "telegram": default})


async def handle_save_telegram(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return web.json_response({"status": "error", "error": "Invalid payload"})
        if not _write_json(TELEGRAM_FILE, data):
            return web.json_response({"status": "error", "error": "Failed to save"})
        bot_state.log("Telegram settings saved", "success")
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_get_owners(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "owners": bot_state.owners,
        "aliases": bot_state.uid_aliases
    })


async def handle_set_owner(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        owner = str(data.get("owner", "")).strip()
        if not uid or not owner:
            return web.json_response({"status": "error", "error": "uid and owner required"})
        bot_state.set_owner(uid, owner)
        _save_owners_from_state()
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_set_alias(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        input_uid = str(data.get("input_uid", "")).strip()
        canonical_uid = str(data.get("canonical_uid", "")).strip()
        if not input_uid or not canonical_uid:
            return web.json_response({"status": "error", "error": "input_uid and canonical_uid required"})
        bot_state.set_alias(input_uid, canonical_uid)
        _save_aliases_from_state()
        _save_owners_from_state()
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_get_config(request: web.Request) -> web.Response:
    default = {
        "maintenance": False, "maint_msg": "We are upgrading the system. Back shortly.",
        "broadcast": {"on": False, "text": "", "type": "info", "id": ""},
        "sales_open": True, "site_name": "RBC LEVEL", "hero_sub": "",
        "plans": None, "logo": ""
    }
    saved = _read_json(CONFIG_FILE, {})
    if isinstance(saved, dict): default.update(saved)
    return web.json_response({"status": "ok", "config": default})


async def handle_save_config(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return web.json_response({"status": "error", "error": "Invalid payload"})
        data["updated_at"] = int(time.time())
        if not _write_json(CONFIG_FILE, data):
            return web.json_response({"status": "error", "error": "Failed to save"})
        bot_state.log("Site config saved", "success")
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


# ==================== LIFECYCLE ====================

async def handle_on_startup(app: web.Application):
    _ensure_data_dir()
    _load_owners_into_state()
    _load_aliases_into_state()
    bot_state.log("Web dashboard started", "success")


async def handle_on_cleanup(app: web.Application):
    _save_owners_from_state()
    _save_aliases_from_state()
    bot_state.log("Web dashboard stopped", "warning")


# ==================== SERVER START ====================

async def start_web_dashboard(host: str = "0.0.0.0", port: int = 5000):
    app = web.Application(middlewares=[cors_middleware])
    app.on_startup.append(handle_on_startup)
    app.on_cleanup.append(handle_on_cleanup)

    # Main
    app.router.add_get("/", handle_index)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/api/health", handle_health)

    # Auth (server-side)
    app.router.add_post("/api/auth/login", handle_auth_login)
    app.router.add_post("/api/user/create", handle_create_user)
    app.router.add_get("/api/creds", handle_get_creds)
    app.router.add_post("/api/creds/save", handle_save_creds)

    # Stats & accounts
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/remove", handle_remove_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/restart", handle_restart_account)
    app.router.add_post("/api/account/stop", handle_stop_account)
    app.router.add_get("/api/console", handle_get_console)

    # Payment proxy
    app.router.add_get("/api/pay/create", handle_pay_create)
    app.router.add_get("/api/pay/verify", handle_pay_verify)

    # Popup / Telegram / Config
    app.router.add_get("/api/public/popup", handle_get_popup)
    app.router.add_post("/api/admin/save-popup", handle_save_popup)
    app.router.add_get("/api/public/telegram", handle_get_telegram)
    app.router.add_post("/api/admin/save-telegram", handle_save_telegram)
    app.router.add_get("/api/public/config", handle_get_config)
    app.router.add_post("/api/admin/save-config", handle_save_config)

    # Owners / Aliases
    app.router.add_get("/api/owners", handle_get_owners)
    app.router.add_post("/api/set-owner", handle_set_owner)
    app.router.add_post("/api/set-alias", handle_set_alias)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    print(f"\033[92m[+] Dashboard on http://{host}:{port}\033[0m")
    print(f"\033[92m[+] Health: /healthz\033[0m")
    print(f"\033[92m[+] Login:  /api/auth/login (server-side)\033[0m")

    return runner


# ==================== STANDALONE ====================

if __name__ == "__main__":
    async def _test_main():
        print("[TEST] dashboard_server.py standalone...")
        port = int(os.getenv("PORT", "20335"))
        runner = await start_web_dashboard(host="0.0.0.0", port=port)
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await runner.cleanup()

    asyncio.run(_test_main())