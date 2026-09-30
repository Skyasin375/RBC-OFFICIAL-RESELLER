# -*- coding: utf-8 -*-
"""
RBC LEVEL UP - Professional Web Dashboard & Real-Time EXP Tracker
Embedded Async Web Server (aiohttp)
Fully working with:
  - Bot state management
  - Account CRUD (add/delete/refresh/restart/stop)
  - Per-account isolation support (owner tracking in accounts.json)
  - UID alias mapping (input UID → canonical bot account_id)
  - Popup notification settings (landing page)
  - Telegram contact settings
  - Console logs endpoint
  - Real profile fields (level/exp/likes) preserved correctly on re-registration
"""

import asyncio
import json
import os
import time
from typing import Dict, List, Any, Optional
from aiohttp import web

# ==================== FILE PATHS ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "index.html")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
POPUP_FILE = os.path.join(DATA_DIR, "popup.json")
TELEGRAM_FILE = os.path.join(DATA_DIR, "telegram.json")
OWNERS_FILE = os.path.join(DATA_DIR, "owners.json")
ALIASES_FILE = os.path.join(DATA_DIR, "aliases.json")


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
        # uid -> email owner map (for per-user isolation)
        self.owners: Dict[str, str] = {}
        # input_uid -> canonical bot account_id map (bridges input vs real UID)
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
        """
        Register or update an account.

        IMPORTANT: `initial_exp` is the baseline used to compute `gained_exp`.
        It must be preserved across re-registrations. Only rebase it when the
        server reports a smaller exp than the current baseline (account reset /
        decay / fresh season) so `gained_exp` never goes artificially negative
        or wildly positive.
        """
        uid_str = str(uid)

        if uid_str not in self.accounts:
            # First time seeing this account — set baseline to current exp
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

            # Update descriptive fields
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if level and level > 0:
                acc["level"] = level

            # --- Preserve initial_exp baseline ---
            if "initial_exp" not in acc or acc.get("initial_exp") is None:
                acc["initial_exp"] = int(exp or 0)
            elif exp and int(exp) < int(acc["initial_exp"]):
                # Server reports a lower exp than our baseline → rebase
                # (e.g. new season, account reset, or first-seen was wrong)
                acc["initial_exp"] = int(exp)

            # --- Only advance current_exp when we have a real (>0) value ---
            # This prevents a failed profile fetch (exp=0 default) from
            # wiping out a good value already in the cache.
            prev_current = int(acc.get("current_exp") or 0)
            if int(exp or 0) > 0:
                acc["current_exp"] = int(exp)
            elif prev_current == 0:
                # Never had a real value — accept the 0 as-is
                acc["current_exp"] = int(exp or 0)

            # --- Recompute gained_exp against preserved baseline ---
            acc["gained_exp"] = max(0, int(acc["current_exp"]) - int(acc["initial_exp"]))

            if likes and likes > 0:
                acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
            acc["owner"] = self.owners.get(uid_str, acc.get("owner", ""))

        self.recalc_totals()

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        """
        Update a running account's exp/level from a live profile fetch.
        Preserves initial_exp baseline; recomputes gained_exp.
        """
        uid_str = str(uid)
        if uid_str not in self.accounts:
            return

        acc = self.accounts[uid_str]
        old_exp = int(acc.get("current_exp") or 0)
        new_exp = int(current_exp or 0)

        # Accept the new value if it's > 0 (real) OR if we had nothing before
        if new_exp > 0 or old_exp == 0:
            acc["current_exp"] = new_exp

        if level is not None and int(level) > 0:
            acc["level"] = int(level)

        # Rebase baseline if server reports an exp lower than it
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
        """
        Called by app.py once the bot resolves the real account_id.
        Maps the input UID (or token prefix) the user provided at add-time to
        the canonical bot account_id. Also transfers ownership so the frontend
        recognizes the real account as belonging to the same user.
        """
        if not input_uid or not canonical_uid:
            return
        input_str = str(input_uid)
        canon_str = str(canonical_uid)
        self.uid_aliases[input_str] = canon_str
        # Also add a self-alias so canonical lookups are stable
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
    """On startup, load owners map from data/owners.json"""
    data = _read_json(OWNERS_FILE, {})
    if isinstance(data, dict):
        bot_state.owners = data
    else:
        bot_state.owners = {}


def _save_owners_from_state():
    _write_json(OWNERS_FILE, bot_state.owners)


def _load_aliases_into_state():
    """On startup, load aliases map from data/aliases.json"""
    data = _read_json(ALIASES_FILE, {})
    if isinstance(data, dict):
        bot_state.uid_aliases = data
    else:
        bot_state.uid_aliases = {}


def _save_aliases_from_state():
    _write_json(ALIASES_FILE, bot_state.uid_aliases)


# ==================== HTTP HANDLERS ====================

async def handle_index(request: web.Request) -> web.Response:
    """Serve the SPA (templates/index.html)."""
    if os.path.exists(TEMPLATE_PATH):
        try:
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                content = f.read()
            return web.Response(text=content, content_type="text/html", charset="utf-8")
        except Exception as e:
            return web.Response(
                text=f"<h1>Error loading template: {e}</h1>",
                content_type="text/html", status=500
            )
    return web.Response(
        text=f"<h1>templates/index.html not found!</h1><p>Expected path: {TEMPLATE_PATH}</p>",
        content_type="text/html", status=404
    )


async def handle_get_stats(request: web.Request) -> web.Response:
    """
    Return all bot stats. Each account includes its owner email.
    Also exposes `owners` (canonical uid -> owner email) and `aliases`
    (input uid -> canonical uid) so the frontend can link input accounts
    to the real bot account_id.
    """
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


async def handle_add_account(request: web.Request) -> web.Response:
    """Add a new account. Body may include `owner` for per-user isolation."""
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
            if owner_email:
                entry["owner"] = owner_email
            existing.append(entry)
            label = uid
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token is required"})
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

        # Set owner in state if provided
        if owner_email:
            bot_state.set_owner(label, owner_email)
            _save_owners_from_state()
            # Seed a self-alias so the alias map has a stable entry from day 1.
            # app.py will overwrite this with the canonical UID after login.
            bot_state.uid_aliases.setdefault(str(label), str(label))
            _save_aliases_from_state()

        # Trigger worker launch if callback registered
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
    """Remove an account permanently."""
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

        # Clean aliases pointing to or from this uid
        changed_alias = False
        for k in list(bot_state.uid_aliases.keys()):
            if k == uid or bot_state.uid_aliases[k] == uid:
                del bot_state.uid_aliases[k]
                changed_alias = True
        if changed_alias:
            _save_aliases_from_state()

        bot_state.log(f"Account {uid} removed from rotation.", "warning", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_remove_account(request: web.Request) -> web.Response:
    """Alias for handle_delete_account — used by the frontend 'Remove' button."""
    return await handle_delete_account(request)


async def handle_refresh_account(request: web.Request) -> web.Response:
    """Trigger a manual refresh of an account's profile."""
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
                bot_state.log(f"on_refresh_account callback error: {e}", "error")

        bot_state.log(f"Refresh requested for {uid}", "info", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_restart_account(request: web.Request) -> web.Response:
    """Restart bot worker for a specific account (cancel + relaunch)."""
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})

        # Cancel existing worker
        if uid in bot_state.account_workers:
            try:
                bot_state.account_workers[uid].cancel()
            except Exception:
                pass
            del bot_state.account_workers[uid]

        # Trigger restart callback
        cb = bot_state.refresh_callbacks.get("on_restart_account")
        if cb:
            try:
                asyncio.create_task(cb(uid))
            except Exception as e:
                bot_state.log(f"on_restart_account error: {e}", "error")

        bot_state.log(f"Restart requested for {uid}", "warning", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_stop_account(request: web.Request) -> web.Response:
    """Stop bot worker for an account without deleting it."""
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
    """Return bot logs for the live console page."""
    return web.json_response({
        "status": "ok",
        "logs": bot_state.logs[-200:],
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "uptime": int(time.time() - bot_state.start_time)
    })


# ==================== POPUP SETTINGS ====================

async def handle_get_popup(request: web.Request) -> web.Response:
    """Return landing-page popup config."""
    default = {
        "enabled": False,
        "header": "Important Update",
        "title": "",
        "message": "",
        "button_text": "Chat Now",
        "button_link": "https://t.me/RexBullYasin",
        "icon_class": "fa-solid fa-circle-check",
        "icon_color": "#22c55e",
        "updated_at": 0
    }
    saved = _read_json(POPUP_FILE, {})
    if isinstance(saved, dict):
        default.update(saved)
    return web.json_response({"status": "ok", "popup": default})


async def handle_save_popup(request: web.Request) -> web.Response:
    """Save popup settings."""
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


# ==================== TELEGRAM SETTINGS ====================

async def handle_get_telegram(request: web.Request) -> web.Response:
    """Return telegram contact config."""
    default = {
        "link": "https://t.me/RexBullYasin",
        "username": "@RexBullYasin"
    }
    saved = _read_json(TELEGRAM_FILE, {})
    if isinstance(saved, dict):
        default.update(saved)
    return web.json_response({"status": "ok", "telegram": default})


async def handle_save_telegram(request: web.Request) -> web.Response:
    """Save telegram config."""
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


# ==================== OWNERS + ALIASES (per-user isolation) ====================

async def handle_get_owners(request: web.Request) -> web.Response:
    """Return uid -> owner_email map (used by frontend)."""
    return web.json_response({
        "status": "ok",
        "owners": bot_state.owners,
        "aliases": bot_state.uid_aliases
    })


async def handle_set_owner(request: web.Request) -> web.Response:
    """Set the owner of a UID (called after add-account from user panel)."""
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
    """
    Manually link an input UID (or token prefix) to a canonical bot account_id.
    Usually called automatically by app.py after login, but exposed here as well.
    """
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


# ==================== PUBLIC CONFIG (CFG bridge for frontend) ====================

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")


async def handle_get_config(request: web.Request) -> web.Response:
    """Return site-wide config (maintenance, broadcast, plans, etc.)."""
    default = {
        "maintenance": False,
        "maint_msg": "We are upgrading the system. Back shortly.",
        "broadcast": {"on": False, "text": "", "type": "info", "id": ""},
        "sales_open": True,
        "site_name": "RBC LEVEL",
        "hero_sub": "",
        "plans": None,
        "logo": ""
    }
    saved = _read_json(CONFIG_FILE, {})
    if isinstance(saved, dict):
        default.update(saved)
    return web.json_response({"status": "ok", "config": default})


async def handle_save_config(request: web.Request) -> web.Response:
    """Save site config (called from admin panel)."""
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
    """
    Start the aiohttp web server.
    All routes are registered inside this function where `app` is defined.
    """
    app = web.Application()

    # Lifecycle hooks
    app.on_startup.append(handle_on_startup)
    app.on_cleanup.append(handle_on_cleanup)

    # ---------- ROUTES ----------
    # Main SPA
    app.router.add_get("/", handle_index)

    # Bot stats & account management
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/remove", handle_remove_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/restart", handle_restart_account)
    app.router.add_post("/api/account/stop", handle_stop_account)

    # Console logs
    app.router.add_get("/api/console", handle_get_console)

    # Popup settings
    app.router.add_get("/api/public/popup", handle_get_popup)
    app.router.add_post("/api/admin/save-popup", handle_save_popup)

    # Telegram settings
    app.router.add_get("/api/public/telegram", handle_get_telegram)
    app.router.add_post("/api/admin/save-telegram", handle_save_telegram)

    # Owners + aliases (per-user isolation)
    app.router.add_get("/api/owners", handle_get_owners)
    app.router.add_post("/api/set-owner", handle_set_owner)
    app.router.add_post("/api/set-alias", handle_set_alias)

    # Site config (CFG bridge)
    app.router.add_get("/api/public/config", handle_get_config)
    app.router.add_post("/api/admin/save-config", handle_save_config)

    # ---------- RUN ----------
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    print(f"\033[92m[+] RBC LEVEL UP Dashboard running on http://localhost:{port}\033[0m")
    print(f"\033[92m[+] Open in browser: http://127.0.0.1:{port}\033[0m")

    return runner


# ==================== STANDALONE TEST ====================

if __name__ == "__main__":
    async def _test_main():
        print("[TEST] Starting dashboard_server.py standalone...")
        runner = await start_web_dashboard(host="0.0.0.0", port=20335)
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await runner.cleanup()
            print("[TEST] Server stopped.")

    asyncio.run(_test_main())