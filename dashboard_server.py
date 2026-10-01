# -*- coding: utf-8 -*-
"""
RBC LEVEL - Dashboard Server (BR + LW Auto-Switch Support)
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

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "skyasinali221@gmail.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "skyasin")

PAY_API_BASE = os.getenv("PAY_API_BASE", "https://fampaygateway.site/api")
PAY_API_KEY = os.getenv("PAY_API_KEY", "FAM_7E06068D658196F192A94D47DF9C46500389DCBB")

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")


def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass


# ==================== EXP TABLE (Lvl → cumulative EXP) ====================
EXP_TABLE: Dict[int, int] = {
    1: 0, 2: 48, 3: 202, 4: 544, 5: 1012, 6: 1844, 7: 2792, 8: 3800,
    9: 4870, 10: 6004, 11: 7192, 12: 8448, 13: 9776, 14: 11140, 15: 12566,
    16: 14060, 17: 15610, 18: 17224, 19: 18902, 20: 20632, 21: 22424,
    22: 24728, 23: 26192, 24: 28166, 25: 30200, 26: 32294, 27: 34448,
    28: 37804, 29: 41174, 30: 44870, 31: 48852, 32: 53334, 33: 58566,
    34: 64096, 35: 69994, 36: 76460, 37: 83108, 38: 91128, 39: 99322,
    40: 108092, 41: 120144, 42: 133266, 43: 147472, 44: 162760,
    45: 179126, 46: 196572, 47: 215368, 48: 235516, 49: 257010,
    50: 279860, 51: 304056, 52: 348318, 53: 394982, 54: 444044,
    55: 495508, 56: 549364, 57: 633756, 58: 721744, 59: 813336,
    60: 908522, 61: 1041438, 62: 1180352, 63: 1325256, 64: 1476184,
    65: 1634300, 66: 1840946, 67: 2056594, 68: 2281242, 69: 2514880,
    70: 2757530, 71: 3059506, 72: 3372284, 73: 3699456, 74: 4041030,
    75: 4397020, 76: 4829104, 77: 5282204, 78: 5756304, 79: 6251404,
    80: 6767504, 81: 7381324, 82: 8043154, 83: 8752952, 84: 9510808,
    85: 10316638, 86: 11277190, 87: 12360748, 88: 13360304,
    89: 14482858, 90: 15659418, 91: 17026708, 92: 18453688,
    93: 19941280, 94: 21488570, 95: 23095858, 96: 24763138,
    97: 26490138, 98: 28277708, 99: 30124996, 100: 32032284
}

# 🔥 MODE THRESHOLD: Level < 3 → BR, Level >= 3 → LONE_WOLF
BR_MAX_LEVEL = 2
LW_MIN_LEVEL = 3


def calculate_level_progress(level: int, current_exp: int) -> Dict[str, Any]:
    level = max(1, min(100, level))
    next_level = min(100, level + 1)
    base_exp = EXP_TABLE.get(level, 0)
    target_exp = EXP_TABLE.get(next_level, base_exp + 50000)

    needed_for_level = max(1, target_exp - base_exp)
    earned_in_level = max(0, current_exp - base_exp)
    remaining_exp = max(0, target_exp - current_exp)
    progress_pct = min(100.0, max(0.0, (earned_in_level / needed_for_level) * 100.0))

    return {
        "next_level": next_level,
        "base_exp": base_exp,
        "target_exp": target_exp,
        "needed_for_level": needed_for_level,
        "earned_in_level": earned_in_level,
        "remaining_exp": remaining_exp,
        "progress_pct": round(progress_pct, 1)
    }


def mode_for_level(level: int) -> str:
    """Return 'BR' if level < 3, else 'LONE_WOLF'."""
    try:
        lvl = int(level or 1)
    except (TypeError, ValueError):
        lvl = 1
    return "BR" if lvl < LW_MIN_LEVEL else "LONE_WOLF"


def mode_label(mode: str) -> str:
    return "Battle Royale (Lvl 1-2)" if mode == "BR" else "Lone Wolf (Lvl 3+)"


# ==================== BOT STATE ====================
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 300
        self.total_matches = 0
        self.total_matches_started = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.owners: Dict[str, str] = {}
        self.uid_aliases: Dict[str, str] = {}
        self.active_writers: Dict[str, set] = {}

    # ---------- Writers (for pause/resume) ----------
    def register_writer(self, uid: str, writer):
        uid_str = str(uid)
        if uid_str not in self.active_writers:
            self.active_writers[uid_str] = set()
        self.active_writers[uid_str].add(writer)

    def unregister_writer(self, uid: str, writer):
        uid_str = str(uid)
        if uid_str in self.active_writers:
            self.active_writers[uid_str].discard(writer)
            if not self.active_writers[uid_str]:
                self.active_writers.pop(uid_str, None)

    def close_writers_for_account(self, uid: str):
        uid_str = str(uid)
        candidates = {uid_str}
        if uid_str in self.uid_aliases:
            candidates.add(str(self.uid_aliases[uid_str]))
        for c in list(candidates):
            writers = list(self.active_writers.get(c, []))
            for w in writers:
                try:
                    if hasattr(w, "close"):
                        if hasattr(w, "is_closing"):
                            if not w.is_closing():
                                w.close()
                        else:
                            w.close()
                except Exception:
                    pass
            self.active_writers.pop(c, None)

    # ---------- Logging ----------
    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "uid": str(uid) if uid else None
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    # ---------- Account registration ----------
    def register_account(self, uid: str, nickname: str, region: str, level: int,
                         exp: int, likes: int = 0, token: Optional[str] = None,
                         auth_uid: Optional[str] = None):
        uid_str = str(uid)
        auth_uid_str = str(auth_uid) if auth_uid else self.uid_aliases.get(uid_str, "")
        if auth_uid_str:
            self.uid_aliases[auth_uid_str] = uid_str
            self.uid_aliases.setdefault(uid_str, uid_str)
        if token:
            self.uid_aliases[token[:16]] = uid_str

        lvl_val = level or 1
        mode = mode_for_level(lvl_val)
        prog = calculate_level_progress(lvl_val, exp)

        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "auth_uid": auth_uid_str or "",
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": lvl_val,
                "next_level": prog["next_level"],
                "mode": mode,
                "mode_label": mode_label(mode),
                "initial_exp": int(exp or 0),
                "current_exp": int(exp or 0),
                "gained_exp": 0,
                "remaining_exp": prog["remaining_exp"],
                "target_exp": prog["target_exp"],
                "needed_for_level": prog["needed_for_level"],
                "earned_in_level": prog["earned_in_level"],
                "progress_pct": prog["progress_pct"],
                "likes": likes or 0,
                "status": "PAUSED" if self.is_paused(uid_str) else "ONLINE",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "token": token or "",
                "start_time": time.time(),
                "is_paused": self.is_paused(uid_str),
                "paused_at": None,
                "total_pause_duration": 0.0,
                "last_updated": time.strftime("%H:%M:%S"),
                "mode_switches": 0,
                "last_mode_switch_at": None
            }
        else:
            acc = self.accounts[uid_str]
            old_level = int(acc.get("level", 1) or 1)
            old_mode = acc.get("mode", mode_for_level(old_level))

            if auth_uid_str:
                acc["auth_uid"] = auth_uid_str
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if level and level > 0:
                acc["level"] = lvl_val
            if token:
                acc["token"] = token

            acc["current_exp"] = int(exp or 0)
            acc["gained_exp"] = max(0, int(exp or 0) - int(acc.get("initial_exp", 0) or 0))
            acc["next_level"] = prog["next_level"]
            acc["remaining_exp"] = prog["remaining_exp"]
            acc["target_exp"] = prog["target_exp"]
            acc["needed_for_level"] = prog["needed_for_level"]
            acc["earned_in_level"] = prog["earned_in_level"]
            acc["progress_pct"] = prog["progress_pct"]
            acc["likes"] = likes or acc.get("likes", 0)

            # 🔥 Mode change detection
            new_mode = mode_for_level(lvl_val)
            acc["mode"] = new_mode
            acc["mode_label"] = mode_label(new_mode)

            if old_mode != new_mode:
                acc["mode_switches"] = int(acc.get("mode_switches", 0)) + 1
                acc["last_mode_switch_at"] = time.strftime("%H:%M:%S")
                if new_mode == "LONE_WOLF":
                    self.log(
                        f"🎉 AUTO-SWITCH! {acc['nickname']} ({uid_str}) reached Level {lvl_val} → "
                        f"BR ➜ LONE WOLF mode activated!",
                        "success", uid_str
                    )
                else:
                    self.log(
                        f"⚠ AUTO-SWITCH! {acc['nickname']} ({uid_str}) dropped to Level {lvl_val} → "
                        f"back to BR mode",
                        "warning", uid_str
                    )

            if not acc.get("is_paused"):
                acc["status"] = acc.get("status", "ONLINE")
            acc["last_updated"] = time.strftime("%H:%M:%S")

        self.recalc_totals()

    # ---------- Helpers ----------
    def get_account_uptime(self, uid_str: str) -> int:
        acc = self.accounts.get(uid_str)
        if not acc and uid_str in self.uid_aliases:
            acc = self.accounts.get(self.uid_aliases[uid_str])
        if not acc:
            return 0
        start_t = acc.get("start_time", time.time())
        total_pause = acc.get("total_pause_duration", 0.0)
        if acc.get("is_paused") and acc.get("paused_at"):
            return max(0, int(acc["paused_at"] - start_t - total_pause))
        return max(0, int(time.time() - start_t - total_pause))

    def is_paused(self, uid: str) -> bool:
        uid_str = str(uid)
        if uid_str in self.accounts:
            return bool(self.accounts[uid_str].get("is_paused"))
        if uid_str in self.uid_aliases:
            canon = self.uid_aliases[uid_str]
            if canon in self.accounts:
                return bool(self.accounts[canon].get("is_paused"))
        return False

    def toggle_pause(self, uid: str) -> bool:
        uid_str = str(uid)
        target_key = uid_str
        if uid_str not in self.accounts and uid_str in self.uid_aliases:
            target_key = self.uid_aliases[uid_str]

        acc = self.accounts.get(target_key)
        if not acc:
            return False

        new_state = not bool(acc.get("is_paused"))
        acc["is_paused"] = new_state
        if new_state:
            acc["paused_at"] = time.time()
            acc["status"] = "PAUSED"
            self.close_writers_for_account(target_key)
            self.log(f"⏸ {acc['nickname']} paused", "warning", target_key)
        else:
            if acc.get("paused_at"):
                acc["total_pause_duration"] = acc.get("total_pause_duration", 0.0) + (time.time() - acc["paused_at"])
            acc["paused_at"] = None
            acc["status"] = "ONLINE"
            self.log(f"▶ {acc['nickname']} resumed", "success", target_key)

        cb = self.refresh_callbacks.get("on_pause_toggle")
        if cb:
            try:
                asyncio.create_task(cb(target_key, new_state))
            except Exception:
                pass

        return new_state

    def toggle_pause_all(self) -> bool:
        if not self.accounts:
            return False
        any_active = any(not acc.get("is_paused") for acc in self.accounts.values())
        for k in list(self.accounts.keys()):
            paused = bool(self.accounts[k].get("is_paused"))
            if any_active and not paused:
                self.toggle_pause(k)
            elif not any_active and paused:
                self.toggle_pause(k)
        return any_active

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str not in self.accounts and uid_str in self.uid_aliases:
            uid_str = self.uid_aliases[uid_str]
        if uid_str not in self.accounts:
            return

        acc = self.accounts[uid_str]
        old_exp = int(acc.get("current_exp", 0) or 0)
        old_level = int(acc.get("level", 1) or 1)
        old_mode = acc.get("mode", mode_for_level(old_level))

        new_exp = int(current_exp or 0)
        if new_exp > 0:
            acc["current_exp"] = new_exp
        new_level = int(level) if (level is not None and int(level) > 0) else old_level
        acc["level"] = new_level

        prog = calculate_level_progress(new_level, acc["current_exp"])
        acc["next_level"] = prog["next_level"]
        acc["remaining_exp"] = prog["remaining_exp"]
        acc["target_exp"] = prog["target_exp"]
        acc["needed_for_level"] = prog["needed_for_level"]
        acc["earned_in_level"] = prog["earned_in_level"]
        acc["progress_pct"] = prog["progress_pct"]
        acc["gained_exp"] = max(0, int(acc["current_exp"]) - int(acc.get("initial_exp", 0) or 0))
        acc["last_updated"] = time.strftime("%H:%M:%S")

        # 🔥 Mode switch check
        new_mode = mode_for_level(new_level)
        acc["mode"] = new_mode
        acc["mode_label"] = mode_label(new_mode)

        if old_mode != new_mode:
            acc["mode_switches"] = int(acc.get("mode_switches", 0)) + 1
            acc["last_mode_switch_at"] = time.strftime("%H:%M:%S")
            if new_mode == "LONE_WOLF":
                self.log(
                    f"🎉 AUTO-SWITCH! {acc['nickname']} ({uid_str}) reached Level {new_level} → "
                    f"BR ➜ LONE WOLF activated!",
                    "success", uid_str
                )
            else:
                self.log(
                    f"⚠ AUTO-SWITCH! {acc['nickname']} ({uid_str}) dropped to Level {new_level} → "
                    f"back to BR",
                    "warning", uid_str
                )

        diff = int(acc["current_exp"]) - old_exp
        if diff > 0:
            self.log(
                f"★ {acc['nickname']} ({uid_str}) +{diff:,} EXP | "
                f"Lvl {new_level} [{new_mode}] ({prog['progress_pct']}%)",
                "success", uid_str
            )

        self.recalc_totals()

    def get_account_level(self, uid: str) -> int:
        uid_str = str(uid)
        if uid_str not in self.accounts and uid_str in self.uid_aliases:
            uid_str = self.uid_aliases[uid_str]
        acc = self.accounts.get(uid_str)
        if acc:
            return int(acc.get("level", 1) or 1)
        return 1

    def get_account_mode(self, uid: str) -> str:
        """Return current mode for a UID: 'BR' or 'LONE_WOLF'."""
        uid_str = str(uid)
        if uid_str not in self.accounts and uid_str in self.uid_aliases:
            uid_str = self.uid_aliases[uid_str]
        acc = self.accounts.get(uid_str)
        if acc:
            return acc.get("mode", mode_for_level(acc.get("level", 1)))
        return "BR"

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str not in self.accounts and uid_str in self.uid_aliases:
            uid_str = self.uid_aliases[uid_str]
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match_started(self):
        self.total_matches_started += 1

    def increment_match(self, uid: str):
        uid_str = str(uid)
        if uid_str not in self.accounts and uid_str in self.uid_aliases:
            uid_str = self.uid_aliases[uid_str]
        self.total_matches += 1
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            acc["matches_played"] = int(acc.get("matches_played", 0)) + 1
            acc["last_match_time"] = time.strftime("%H:%M:%S")
            acc["last_updated"] = time.strftime("%H:%M:%S")
            self.log(
                f"⚔ Match #{acc['matches_played']} finished for {acc['nickname']} ({uid_str}) [{acc.get('mode','BR')}]",
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
        if not input_uid or not canonical_uid:
            return
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
    if not os.path.exists(path):
        return default
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


# ==================== CORS ====================
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
    return web.Response(text="<h1>templates/index.html not found</h1>", content_type="text/html", status=404)


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

    for acc in accounts_data:
        uid_k = str(acc.get("uid", ""))
        acc["uptime_seconds"] = bot_state.get_account_uptime(uid_k)
        acc["is_paused"] = bot_state.is_paused(uid_k)
        # ensure mode is always present
        if "mode" not in acc:
            acc["mode"] = mode_for_level(acc.get("level", 1))
        if "mode_label" not in acc:
            acc["mode_label"] = mode_label(acc["mode"])

    return web.json_response({
        "status": "ok",
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_matches_started": bot_state.total_matches_started,
        "total_gained_exp": bot_state.total_gained_exp,
        "accounts": accounts_data,
        "owners": bot_state.owners,
        "aliases": bot_state.uid_aliases,
        "logs": bot_state.logs[-80:],
        "uptime": int(time.time() - bot_state.start_time)
    })


# ==================== AUTH ====================
async def handle_auth_login(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        if not email or not password:
            return web.json_response({"status": "error", "error": "email & password required"})

        if email == ADMIN_EMAIL.lower() and password == ADMIN_PASSWORD:
            bot_state.log(f"Admin login: {email}", "success")
            return web.json_response({"status": "ok", "role": "admin", "email": ADMIN_EMAIL})

        users = _load_users()
        user = next((u for u in users if str(u.get("email", "")).lower() == email), None)
        if not user or user.get("password") != password:
            return web.json_response({"status": "error", "error": "Invalid email or password"}, status=401)

        creds = _load_creds()
        cred = next((c for c in creds if str(c.get("email", "")).lower() == email), None)
        if not cred:
            return web.json_response({"status": "error", "error": "No plan found"}, status=403)
        if cred.get("revoked"):
            return web.json_response({"status": "error", "error": "Credentials revoked"}, status=403)
        exp = cred.get("expiresAt") or 0
        if exp and exp > 0 and int(time.time() * 1000) > exp:
            return web.json_response({"status": "error", "error": "Plan expired"}, status=403)

        bot_state.log(f"User login: {email}", "success")
        return web.json_response({
            "status": "ok", "role": "user", "email": email,
            "user": user, "credential": cred
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def handle_create_user(request: web.Request) -> web.Response:
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
        users.append({"name": "User", "email": email, "password": password, "createdAt": now_ms})
        _save_users(users)

        creds = _load_creds()
        creds = [c for c in creds if str(c.get("email", "")).lower() != email]
        creds.insert(0, {
            "email": email, "password": password, "slots": slots,
            "label": label, "revoked": False,
            "created_at": now_ms, "expiresAt": expires_at, "days": days
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
            return web.json_response({"status": "error", "error": "credentials must be list"})
        _save_creds(creds)
        users = [{
            "name": "User", "email": c.get("email", ""),
            "password": c.get("password", ""),
            "createdAt": c.get("created_at", int(time.time() * 1000))
        } for c in creds if c.get("email")]
        _save_users(users)
        bot_state.log(f"Credentials updated ({len(creds)})", "success")
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
                return web.json_response({"status": "error", "error": "UID and Password required"})
            existing = [acc for acc in existing if str(acc.get("uid")) != uid]
            entry = {"uid": uid, "password": pwd}
            if owner_email:
                entry["owner"] = owner_email
            existing.append(entry)
            label = uid
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token required"})
            existing = [acc for acc in existing if acc.get("token") != token]
            entry = {"token": token}
            if owner_email:
                entry["owner"] = owner_email
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
                bot_state.log(f"on_account_added cb error: {e}", "error")

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
            try:
                bot_state.account_workers[uid].cancel()
            except Exception:
                pass
            del bot_state.account_workers[uid]
        if uid in bot_state.owners:
            del bot_state.owners[uid]
            _save_owners_from_state()

        changed = False
        for k in list(bot_state.uid_aliases.keys()):
            if k == uid or bot_state.uid_aliases[k] == uid:
                del bot_state.uid_aliases[k]
                changed = True
        if changed:
            _save_aliases_from_state()

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
            try:
                asyncio.create_task(cb(uid))
            except Exception as e:
                bot_state.log(f"refresh cb error: {e}", "error")
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
            try:
                bot_state.account_workers[uid].cancel()
            except Exception:
                pass
            del bot_state.account_workers[uid]
        cb = bot_state.refresh_callbacks.get("on_restart_account")
        if cb:
            try:
                asyncio.create_task(cb(uid))
            except Exception as e:
                bot_state.log(f"restart cb error: {e}", "error")
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
            try:
                bot_state.account_workers[uid].cancel()
            except Exception:
                pass
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


# ==================== MODE / PAUSE ====================
async def handle_toggle_pause(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})
        is_paused = bot_state.toggle_pause(uid)
        return web.json_response({"status": "ok", "is_paused": is_paused})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_toggle_pause_all(request: web.Request) -> web.Response:
    try:
        paused_state = bot_state.toggle_pause_all()
        return web.json_response({"status": "ok", "all_paused": paused_state})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_get_mode(request: web.Request) -> web.Response:
    """Get current mode for a UID."""
    try:
        uid = request.query.get("uid", "").strip()
        if not uid:
            return web.json_response({"status": "error", "error": "uid query required"})
        mode = bot_state.get_account_mode(uid)
        level = bot_state.get_account_level(uid)
        return web.json_response({
            "status": "ok", "uid": uid,
            "mode": mode, "mode_label": mode_label(mode), "level": level
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


# ==================== PAYMENT ====================
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
    if isinstance(saved, dict):
        default.update(saved)
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
    if isinstance(saved, dict):
        default.update(saved)
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
        "maintenance": False,
        "maint_msg": "We are upgrading the system. Back shortly.",
        "broadcast": {"on": False, "text": "", "type": "info", "id": ""},
        "sales_open": True, "site_name": "RBC LEVEL", "hero_sub": "",
        "plans": None, "logo": ""
    }
    saved = _read_json(CONFIG_FILE, {})
    if isinstance(saved, dict):
        default.update(saved)
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
    bot_state.log("Web dashboard started (BR + LW Auto-Switch)", "success")


async def handle_on_cleanup(app: web.Application):
    _save_owners_from_state()
    _save_aliases_from_state()
    bot_state.log("Web dashboard stopped", "warning")


# ==================== SERVER START ====================
async def start_web_dashboard(host: str = "0.0.0.0", port: int = 5000):
    app = web.Application(middlewares=[cors_middleware])
    app.on_startup.append(handle_on_startup)
    app.on_cleanup.append(handle_on_cleanup)

    app.router.add_get("/", handle_index)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/api/health", handle_health)

    # Auth
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

    # 🔥 Mode & Pause control
    app.router.add_get("/api/account/mode", handle_get_mode)
    app.router.add_post("/api/account/pause", handle_toggle_pause)
    app.router.add_post("/api/account/pause_all", handle_toggle_pause_all)

    # Payment
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
    print(f"\033[92m[+] Mode API: /api/account/mode?uid=XXX\033[0m")
    print(f"\033[92m[+] Pause API: /api/account/pause\033[0m")

    return runner


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