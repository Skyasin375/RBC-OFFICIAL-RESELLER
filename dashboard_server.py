# -*- coding: utf-8 -*-
"""
FreeFire Level Up Bot — Dashboard Server (FULL FIXED VERSION)
Features:
  - Server-side credentials (cross-device login)
  - Persistent owner mapping (survives refresh)
  - Redis support (optional) + file fallback
  - BR / Lone Wolf mode tracking
  - Pause / Resume / Stop / Restart / Delete
  - Writer registry, dual-ID mapping
  - Real EXP progress
"""

import asyncio
import json
import os
import time
from typing import Dict, List, Any, Optional, Set
from aiohttp import web

# ==================== PATHS (Render-aware) ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "index.html")

DATA_DIR = os.getenv("DATA_DIR", ".")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
OWNERS_FILE = os.path.join(DATA_DIR, "owners.json")
ALIASES_FILE = os.path.join(DATA_DIR, "aliases.json")
CREDS_FILE = os.path.join(DATA_DIR, "credentials.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")

# ==================== REDIS (Optional) ====================
_REDIS = None
_REDIS_TRIED = False

def _get_redis():
    global _REDIS, _REDIS_TRIED
    if _REDIS_TRIED:
        return _REDIS
    _REDIS_TRIED = True
    try:
        from upstash_redis import Redis
        url = os.getenv("UPSTASH_REDIS_REST_URL")
        token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
        if url and token:
            _REDIS = Redis(url=url, token=token)
            print("[REDIS] Connected to Upstash", flush=True)
        else:
            print("[REDIS] No env vars → using file storage", flush=True)
    except ImportError:
        print("[REDIS] upstash-redis not installed → using file storage", flush=True)
    except Exception as e:
        print(f"[REDIS] Init failed: {e} → using file storage", flush=True)
    return _REDIS

# ==================== EXP TABLE ====================
EXP_TABLE: Dict[int, int] = {
    1: 0, 2: 48, 3: 202, 4: 544, 5: 1012, 6: 1844, 7: 2792, 8: 3800,
    9: 4870, 10: 6004, 11: 7192, 12: 8448, 13: 9760, 14: 11140, 15: 12566,
    16: 14060, 17: 15610, 18: 17224, 19: 18902, 20: 20632, 21: 22424, 22: 24278,
    23: 26192, 24: 28166, 25: 30200, 26: 32294, 27: 34448, 28: 37804, 29: 41274,
    30: 44870, 31: 48582, 32: 53394, 33: 58566, 34: 64096, 35: 69994, 36: 76460,
    37: 83506, 38: 91128, 39: 99322, 40: 108092, 41: 120144, 42: 133266, 43: 147472,
    44: 162760, 45: 179126, 46: 196572, 47: 215368, 48: 235516, 49: 257010, 50: 279860,
    51: 304056, 52: 348318, 53: 394982, 54: 444044, 55: 495508, 56: 549364, 57: 633756,
    58: 721744, 59: 813336, 60: 908522, 61: 1041438, 62: 1180352, 63: 1325266,
    64: 1476184, 65: 1634300, 66: 1840946, 67: 2056594, 68: 2281242, 69: 2514880,
    70: 2757530, 71: 3059506, 72: 3372284, 73: 3699456, 74: 4041030, 75: 4397002,
    76: 4829104, 77: 5282204, 78: 5756304, 79: 6251408, 80: 6776502, 81: 7381324,
    82: 8043154, 83: 8752982, 84: 9510808, 85: 10316338, 86: 11277190, 87: 12291748,
    88: 13360304, 89: 14482858, 90: 15659418, 91: 17026708, 92: 18453950, 93: 19941280,
    94: 21488570, 95: 23095858, 96: 24763138, 97: 26490428, 98: 28378704, 99: 30124996,
    100: 32032884
}

MODE_SWITCH_LEVEL = 3


def calculate_level_progress(level: int, current_exp: int) -> Dict[str, Any]:
    try:
        level = max(1, min(100, int(level or 1)))
    except (ValueError, TypeError):
        level = 1
    try:
        current_exp = max(0, int(current_exp or 0))
    except (ValueError, TypeError):
        current_exp = 0

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


# ==================== FILE HELPERS ====================
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
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[DASH] write error {path}: {e}", flush=True)
        return False


# ==================== CREDENTIALS (Server-side) ====================
def get_credential(email: str) -> Optional[Dict[str, Any]]:
    email = str(email).lower().strip()
    redis = _get_redis()
    if redis:
        try:
            data = redis.hgetall(f"cred:{email}")
            if data and data.get("email"):
                return _normalize_cred(data)
            return None
        except Exception as e:
            print(f"[CREDS] Redis get failed: {e}", flush=True)

    creds = _read_json(CREDS_FILE, [])
    for c in creds:
        if str(c.get("email", "")).lower() == email:
            return c
    return None


def list_credentials() -> List[Dict[str, Any]]:
    redis = _get_redis()
    if redis:
        try:
            emails = redis.smembers("all_creds") or []
            out = []
            for e in emails:
                data = redis.hgetall(f"cred:{e}")
                if data and data.get("email"):
                    out.append(_normalize_cred(data))
            out.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            return out
        except Exception as e:
            print(f"[CREDS] Redis list failed: {e}", flush=True)

    return _read_json(CREDS_FILE, [])


def save_credential(payload: Dict[str, Any]) -> bool:
    email = str(payload.get("email", "")).lower().strip()
    if not email:
        return False

    redis = _get_redis()
    if redis:
        try:
            redis.hset(f"cred:{email}", mapping={
                "email": email,
                "password": str(payload.get("password", "")),
                "slots": str(payload.get("slots", 3)),
                "label": str(payload.get("label", "")),
                "days": str(payload.get("days", 0)),
                "expiresAt": str(payload.get("expiresAt", 0)),
                "revoked": "1" if payload.get("revoked") else "0",
                "created_at": str(payload.get("created_at", int(time.time() * 1000))),
            })
            redis.sadd("all_creds", email)
            return True
        except Exception as e:
            print(f"[CREDS] Redis save failed: {e}", flush=True)

    creds = _read_json(CREDS_FILE, [])
    creds = [c for c in creds if str(c.get("email", "")).lower() != email]
    creds.insert(0, payload)
    return _write_json(CREDS_FILE, creds)


def delete_credential(email: str) -> bool:
    email = str(email).lower().strip()
    redis = _get_redis()
    if redis:
        try:
            redis.delete(f"cred:{email}")
            redis.srem("all_creds", email)
            return True
        except Exception as e:
            print(f"[CREDS] Redis delete failed: {e}", flush=True)

    creds = _read_json(CREDS_FILE, [])
    creds = [c for c in creds if str(c.get("email", "")).lower() != email]
    return _write_json(CREDS_FILE, creds)


def set_credential_revoked(email: str, revoked: bool) -> bool:
    email = str(email).lower().strip()
    redis = _get_redis()
    if redis:
        try:
            redis.hset(f"cred:{email}", {"revoked": "1" if revoked else "0"})
            return True
        except Exception as e:
            print(f"[CREDS] Redis revoke failed: {e}", flush=True)

    creds = _read_json(CREDS_FILE, [])
    for c in creds:
        if str(c.get("email", "")).lower() == email:
            c["revoked"] = revoked
    return _write_json(CREDS_FILE, creds)


def _normalize_cred(data: Dict) -> Dict:
    return {
        "email": data.get("email", ""),
        "password": data.get("password", ""),
        "slots": int(data.get("slots", 3) or 3),
        "label": data.get("label", ""),
        "days": int(data.get("days", 0) or 0),
        "expiresAt": int(data.get("expiresAt", 0) or 0),
        "revoked": data.get("revoked") == "1" or data.get("revoked") is True,
        "created_at": int(data.get("created_at", 0) or 0),
    }


# ==================== OWNER / ALIAS (Server-side) ====================
def get_all_owners() -> Dict[str, str]:
    redis = _get_redis()
    if redis:
        try:
            data = redis.hgetall("owners") or {}
            if data:
                return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            print(f"[OWNERS] Redis get failed: {e}", flush=True)
    return _read_json(OWNERS_FILE, {})


def set_owner(uid: str, owner_email: str):
    uid_str = str(uid).strip()
    owner = str(owner_email).lower().strip()
    if not uid_str or not owner:
        return
    redis = _get_redis()
    if redis:
        try:
            redis.hset("owners", {uid_str: owner})
        except Exception as e:
            print(f"[OWNERS] Redis set failed: {e}", flush=True)
    owners = _read_json(OWNERS_FILE, {})
    owners[uid_str] = owner
    _write_json(OWNERS_FILE, owners)


def get_all_aliases() -> Dict[str, str]:
    redis = _get_redis()
    if redis:
        try:
            data = redis.hgetall("aliases") or {}
            if data:
                return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            print(f"[ALIASES] Redis get failed: {e}", flush=True)
    return _read_json(ALIASES_FILE, {})


def set_alias(input_uid: str, canonical_uid: str):
    inp = str(input_uid).strip()
    can = str(canonical_uid).strip()
    if not inp or not can:
        return
    redis = _get_redis()
    if redis:
        try:
            redis.hset("aliases", {inp: can, can: can})
        except Exception as e:
            print(f"[ALIASES] Redis set failed: {e}", flush=True)
    aliases = _read_json(ALIASES_FILE, {})
    aliases[inp] = can
    aliases.setdefault(can, can)
    _write_json(ALIASES_FILE, aliases)


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
        self.account_token_map: Dict[str, str] = {}
        self.auth_to_game_id: Dict[str, str] = {}
        self.game_to_auth_id: Dict[str, str] = {}
        self.paused_accounts: Set[str] = set()
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.active_writers: Dict[str, Set[Any]] = {}
        self.owners: Dict[str, str] = {}
        self.uid_aliases: Dict[str, str] = {}

    # ---------- Owner / Alias persistence ----------
    def load_owners_from_disk(self):
        self.owners = get_all_owners() or {}
        print(f"[OWNERS] Loaded {len(self.owners)} mappings", flush=True)

    def save_owners_to_disk(self):
        for uid, owner in self.owners.items():
            try:
                set_owner(uid, owner)
            except Exception:
                pass

    def load_aliases_from_disk(self):
        self.uid_aliases = get_all_aliases() or {}
        print(f"[ALIASES] Loaded {len(self.uid_aliases)} mappings", flush=True)

    def save_aliases_to_disk(self):
        for inp, can in self.uid_aliases.items():
            try:
                set_alias(inp, can)
            except Exception:
                pass

    def set_owner(self, uid: str, owner_email: str):
        if not uid or not owner_email:
            return
        uid_str = str(uid)
        owner = str(owner_email).lower().strip()
        self.owners[uid_str] = owner
        if uid_str in self.accounts:
            self.accounts[uid_str]["owner"] = owner
        try:
            set_owner(uid_str, owner)
        except Exception:
            pass

    def set_alias(self, input_uid: str, canonical_uid: str):
        if not input_uid or not canonical_uid:
            return
        inp = str(input_uid)
        can = str(canonical_uid)
        self.uid_aliases[inp] = can
        self.uid_aliases.setdefault(can, can)
        owner = self.owners.get(inp)
        if owner:
            self.owners[can] = owner
            if can in self.accounts:
                self.accounts[can]["owner"] = owner
            try:
                set_owner(can, owner)
            except Exception:
                pass
        try:
            set_alias(inp, can)
        except Exception:
            pass

    # ---------- Writer registry ----------
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
        if uid_str in self.auth_to_game_id:
            candidates.add(str(self.auth_to_game_id[uid_str]))
        if uid_str in self.game_to_auth_id:
            candidates.add(str(self.game_to_auth_id[uid_str]))
        if uid_str in self.account_token_map:
            mapped = self.account_token_map[uid_str]
            candidates.add(str(mapped))
            candidates.add(str(mapped)[:16])

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
    def register_account(self, uid: str, nickname: str, region: str, level: int, exp: int,
                         likes: int = 0, token: Optional[str] = None,
                         auth_uid: Optional[str] = None, owner: str = ""):
        uid_str = str(uid)
        auth_uid_str = str(auth_uid) if auth_uid else self.game_to_auth_id.get(uid_str, "")

        if auth_uid_str:
            self.auth_to_game_id[auth_uid_str] = uid_str
            self.game_to_auth_id[uid_str] = auth_uid_str
            self.account_token_map[auth_uid_str] = uid_str
            self.account_token_map[uid_str] = auth_uid_str

        if token:
            self.account_token_map[uid_str] = token
            self.account_token_map[token[:16]] = uid_str
            if auth_uid_str:
                self.account_token_map[auth_uid_str] = token

        # Resolve owner: explicit > existing > auth_uid mapping
        owner_final = (
            owner
            or self.owners.get(uid_str)
            or (self.owners.get(auth_uid_str) if auth_uid_str else "")
            or ""
        )
        owner_final = str(owner_final).lower().strip()

        if owner_final:
            self.owners[uid_str] = owner_final
            if auth_uid_str:
                self.owners[auth_uid_str] = owner_final
            try:
                set_owner(uid_str, owner_final)
                if auth_uid_str:
                    set_owner(auth_uid_str, owner_final)
            except Exception:
                pass

        try:
            lvl_val = max(1, int(level or 1))
        except (ValueError, TypeError):
            lvl_val = 1
        try:
            exp_val = max(0, int(exp or 0))
        except (ValueError, TypeError):
            exp_val = 0

        prog = calculate_level_progress(lvl_val, exp_val)
        acc_mode = "BR" if lvl_val < MODE_SWITCH_LEVEL else "LONE_WOLF"
        acc_mode_label = (
            f"Battle Royale (Lvl < {MODE_SWITCH_LEVEL})"
            if lvl_val < MODE_SWITCH_LEVEL
            else f"Lone Wolf (Lvl ≥ {MODE_SWITCH_LEVEL})"
        )

        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "auth_uid": auth_uid_str or "",
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": lvl_val,
                "next_level": prog["next_level"],
                "mode": acc_mode,
                "mode_label": acc_mode_label,
                "initial_exp": exp_val,
                "current_exp": exp_val,
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
                "owner": owner_final,
                "start_time": time.time(),
                "is_paused": self.is_paused(uid_str),
                "paused_at": time.time() if self.is_paused(uid_str) else None,
                "total_pause_duration": 0.0,
                "last_updated": time.strftime("%H:%M:%S")
            }
        else:
            acc = self.accounts[uid_str]
            if auth_uid_str:
                acc["auth_uid"] = auth_uid_str
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if lvl_val:
                acc["level"] = lvl_val
            if token:
                acc["token"] = token
            if owner_final:
                acc["owner"] = owner_final
            acc["current_exp"] = exp_val
            acc["gained_exp"] = max(0, exp_val - acc.get("initial_exp", exp_val))
            acc["next_level"] = prog["next_level"]
            acc["remaining_exp"] = prog["remaining_exp"]
            acc["target_exp"] = prog["target_exp"]
            acc["needed_for_level"] = prog["needed_for_level"]
            acc["earned_in_level"] = prog["earned_in_level"]
            acc["progress_pct"] = prog["progress_pct"]
            acc["likes"] = likes
            acc["mode"] = acc_mode
            acc["mode_label"] = acc_mode_label
            if not acc.get("is_paused"):
                acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")

        self.recalc_totals()

    def get_account_uptime(self, uid_str: str) -> int:
        acc = self.accounts.get(uid_str)
        if not acc:
            mapped = self.game_to_auth_id.get(uid_str) or self.auth_to_game_id.get(uid_str)
            if mapped and mapped in self.accounts:
                acc = self.accounts[mapped]
        if not acc:
            return 0
        start_t = acc.get("start_time", time.time())
        total_pause = acc.get("total_pause_duration", 0.0)
        if acc.get("is_paused") and acc.get("paused_at"):
            return max(0, int(acc["paused_at"] - start_t - total_pause))
        return max(0, int(time.time() - start_t - total_pause))

    # ---------- Pause / Resume ----------
    def is_paused(self, uid: str) -> bool:
        uid_str = str(uid)
        if uid_str in self.paused_accounts:
            return True
        game_id = self.auth_to_game_id.get(uid_str)
        if game_id and game_id in self.paused_accounts:
            return True
        auth_uid = self.game_to_auth_id.get(uid_str)
        if auth_uid and auth_uid in self.paused_accounts:
            return True
        acc = self.accounts.get(uid_str) or (self.accounts.get(game_id) if game_id else None)
        if acc and acc.get("is_paused"):
            return True
        return False

    def toggle_pause(self, uid: str) -> bool:
        uid_str = str(uid)
        candidates = {uid_str}
        if uid_str in self.auth_to_game_id:
            candidates.add(self.auth_to_game_id[uid_str])
        if uid_str in self.game_to_auth_id:
            candidates.add(self.game_to_auth_id[uid_str])

        target_acc = None
        target_key = uid_str
        for c in candidates:
            if c in self.accounts:
                target_acc = self.accounts[c]
                target_key = c
                break

        is_now_paused = not self.is_paused(uid_str)
        if is_now_paused:
            for c in candidates:
                self.paused_accounts.add(c)
                self.close_writers_for_account(c)
            if target_acc:
                target_acc["is_paused"] = True
                target_acc["paused_at"] = time.time()
                target_acc["status"] = "PAUSED"
            nick = target_acc.get("nickname", target_key) if target_acc else target_key
            self.log(f"⏸ UID {target_key} ({nick}) PAUSED.", "warning", target_key)
            cb = self.refresh_callbacks.get("on_pause_toggle")
            if cb:
                try:
                    asyncio.create_task(cb(target_key, True))
                except Exception:
                    pass
        else:
            for c in candidates:
                self.paused_accounts.discard(c)
            if target_acc:
                target_acc["is_paused"] = False
                if target_acc.get("paused_at"):
                    pause_dur = time.time() - target_acc["paused_at"]
                    target_acc["total_pause_duration"] = \
                        target_acc.get("total_pause_duration", 0.0) + pause_dur
                    target_acc["paused_at"] = None
                target_acc["status"] = "ONLINE"
            nick = target_acc.get("nickname", target_key) if target_acc else target_key
            self.log(f"▶ UID {target_key} ({nick}) RESUMED.", "success", target_key)
            cb = self.refresh_callbacks.get("on_pause_toggle")
            if cb:
                try:
                    asyncio.create_task(cb(target_key, False))
                except Exception:
                    pass

        return is_now_paused

    def toggle_pause_all(self) -> bool:
        any_active = any(not self.is_paused(k) for k in self.accounts.keys())
        for k in list(self.accounts.keys()):
            current_paused = self.is_paused(k)
            if any_active and not current_paused:
                self.toggle_pause(k)
            elif not any_active and current_paused:
                self.toggle_pause(k)
        return any_active

    # ---------- EXP / Level ----------
    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str not in self.accounts:
            return
        acc = self.accounts[uid_str]
        old_exp = acc.get("current_exp", 0)
        old_level = acc.get("level", 1)

        try:
            current_exp = max(0, int(current_exp or 0))
        except (ValueError, TypeError):
            current_exp = old_exp
        try:
            if level is not None:
                level = max(1, int(level))
        except (ValueError, TypeError):
            level = old_level

        acc["current_exp"] = current_exp
        if level is not None and level > 0:
            acc["level"] = level
        acc["gained_exp"] = max(0, current_exp - acc.get("initial_exp", 0))

        current_lvl = acc["level"]
        acc["mode"] = "BR" if current_lvl < MODE_SWITCH_LEVEL else "LONE_WOLF"
        acc["mode_label"] = (
            f"Battle Royale (Lvl < {MODE_SWITCH_LEVEL})"
            if current_lvl < MODE_SWITCH_LEVEL
            else f"Lone Wolf (Lvl ≥ {MODE_SWITCH_LEVEL})"
        )

        prog = calculate_level_progress(current_lvl, current_exp)
        acc["next_level"] = prog["next_level"]
        acc["remaining_exp"] = prog["remaining_exp"]
        acc["target_exp"] = prog["target_exp"]
        acc["needed_for_level"] = prog["needed_for_level"]
        acc["earned_in_level"] = prog["earned_in_level"]
        acc["progress_pct"] = prog["progress_pct"]
        acc["last_updated"] = time.strftime("%H:%M:%S")

        if old_level < MODE_SWITCH_LEVEL <= current_lvl:
            self.log(
                f"🎉 LEVEL UP! UID {uid_str} ({acc['nickname']}) reached Level {current_lvl}! "
                f"Switching BR → Lone Wolf.",
                "success", uid_str
            )

        diff = current_exp - old_exp
        if diff > 0:
            self.log(
                f"★ UID {uid_str} ({acc['nickname']}) gained +{diff:,} EXP | "
                f"Level {acc['level']} [{acc['mode']}] "
                f"({prog['progress_pct']}% · {prog['remaining_exp']:,} EXP to Lvl {prog['next_level']})",
                "success", uid_str
            )
        self.recalc_totals()

    def get_account_level(self, uid: str) -> int:
        uid_str = str(uid)
        acc = self.accounts.get(uid_str)
        if not acc:
            mapped = self.game_to_auth_id.get(uid_str) or self.auth_to_game_id.get(uid_str)
            if mapped and mapped in self.accounts:
                acc = self.accounts[mapped]
        if acc:
            try:
                return int(acc.get("level", 1) or 1)
            except (ValueError, TypeError):
                return 1
        return 1

    def get_account_mode(self, uid: str) -> str:
        lvl = self.get_account_level(uid)
        return "BR" if lvl < MODE_SWITCH_LEVEL else "LONE_WOLF"

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match_started(self):
        self.total_matches_started += 1

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] = \
                int(self.accounts[uid_str].get("matches_played", 0)) + 1
            self.accounts[uid_str]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
            self.log(
                f"⚔ Match #{self.accounts[uid_str]['matches_played']} finished for "
                f"{self.accounts[uid_str]['nickname']} ({uid_str})",
                "info", uid_str
            )

    def recalc_totals(self):
        self.total_gained_exp = sum(
            int(acc.get("gained_exp", 0) or 0) for acc in self.accounts.values()
        )


bot_state = BotState()


# ==================== FALLBACK HTML ====================
FALLBACK_INDEX_HTML = """<!DOCTYPE html>
<html>
<head><title>RBC Level Bot</title></head>
<body style="background:#0a0a12;color:#fff;font-family:sans-serif;text-align:center;padding:50px;">
<h1>BOT RUNNING</h1>
<p>templates/index.html is loading...</p>
</body>
</html>"""


# ==================== HANDLERS ====================
async def handle_index(request: web.Request) -> web.Response:
    content = FALLBACK_INDEX_HTML
    if os.path.exists(TEMPLATE_PATH):
        try:
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            pass
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_get_stats(request: web.Request) -> web.Response:
    accounts_data = list(bot_state.accounts.values())
    accounts_data.sort(key=lambda x: int(x.get("gained_exp", 0) or 0), reverse=True)
    uptime_sec = max(1, int(time.time() - bot_state.start_time))
    total_gained = bot_state.total_gained_exp
    exp_per_hour = int((total_gained / uptime_sec) * 3600)
    total_active_matches = sum(int(acc.get("active_matches", 0) or 0) for acc in accounts_data)

    for acc in accounts_data:
        uid_k = str(acc.get("uid", ""))
        acc["uptime_seconds"] = bot_state.get_account_uptime(uid_k)
        acc["is_paused"] = bot_state.is_paused(uid_k)
        acc["mode"] = bot_state.get_account_mode(uid_k)
        # Resolve owner: uid → alias → auth_uid
        owner = bot_state.owners.get(uid_k, "")
        if not owner:
            auth_uid = acc.get("auth_uid", "")
            if auth_uid:
                owner = bot_state.owners.get(str(auth_uid), "")
        if not owner:
            for inp, can in bot_state.uid_aliases.items():
                if str(can) == uid_k and inp in bot_state.owners:
                    owner = bot_state.owners[inp]
                    break
        acc["owner"] = owner

    return web.json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_matches_started": bot_state.total_matches_started,
        "total_active_matches": total_active_matches,
        "total_gained_exp": total_gained,
        "exp_per_hour": exp_per_hour,
        "accounts": accounts_data,
        "owners": dict(bot_state.owners),
        "aliases": dict(bot_state.uid_aliases),
        "logs": bot_state.logs[-80:],
        "uptime": uptime_sec,
        "mode_switch_level": MODE_SWITCH_LEVEL,
    })


# ==================== AUTH ====================
ADMIN_EMAIL = "skyasinali221@gmail.com"
ADMIN_PASSWORD = "skyasin"


async def handle_login(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        email = str(data.get("email", "")).lower().strip()
        password = str(data.get("password", ""))

        if not email or not password:
            return web.json_response({"status": "error", "error": "Email & password required"})

        # Admin check
        if email == ADMIN_EMAIL.lower() and password == ADMIN_PASSWORD:
            return web.json_response({
                "status": "ok",
                "role": "admin",
                "credential": {
                    "email": ADMIN_EMAIL,
                    "slots": 9999,
                    "expiresAt": 0,
                    "label": "ADMIN"
                }
            })

        # User check from storage
        cred = get_credential(email)
        if not cred:
            return web.json_response({"status": "error", "error": "Invalid credentials"})
        if str(cred.get("password", "")) != password:
            return web.json_response({"status": "error", "error": "Invalid credentials"})
        if cred.get("revoked"):
            return web.json_response({"status": "error", "error": "Account revoked"})
        exp = int(cred.get("expiresAt", 0) or 0)
        if exp > 0 and time.time() * 1000 > exp:
            return web.json_response({"status": "error", "error": "Plan expired"})

        return web.json_response({
            "status": "ok",
            "role": "user",
            "credential": {
                "email": email,
                "slots": int(cred.get("slots", 3) or 3),
                "expiresAt": exp,
                "label": cred.get("label", "")
            }
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_save_credential(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        email = str(data.get("email", "")).lower().strip()
        if not email or not data.get("password"):
            return web.json_response({"status": "error", "error": "Missing fields"})

        ok = save_credential({
            "email": email,
            "password": str(data.get("password", "")),
            "slots": int(data.get("slots", 3) or 3),
            "label": str(data.get("label", "")),
            "days": int(data.get("days", 0) or 0),
            "expiresAt": int(data.get("expiresAt", 0) or 0),
            "revoked": bool(data.get("revoked", False)),
            "created_at": int(time.time() * 1000),
        })
        if not ok:
            return web.json_response({"status": "error", "error": "Save failed"})
        return web.json_response({"status": "ok", "email": email})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_list_credentials(request: web.Request) -> web.Response:
    try:
        creds = list_credentials()
        return web.json_response({"status": "ok", "credentials": creds})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_credential_action(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        email = str(data.get("email", "")).lower().strip()
        action = data.get("action", "")
        if not email or not action:
            return web.json_response({"status": "error", "error": "Missing fields"})

        if action == "delete":
            delete_credential(email)
            return web.json_response({"status": "ok", "action": "deleted"})
        if action == "revoke":
            set_credential_revoked(email, True)
            return web.json_response({"status": "ok", "action": "revoked"})
        if action == "restore":
            set_credential_revoked(email, False)
            return web.json_response({"status": "ok", "action": "restored"})
        return web.json_response({"status": "error", "error": "Unknown action"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


# ==================== ACCOUNT HANDLERS ====================
async def handle_add_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        owner_email = str(data.get("owner", "")).lower().strip()

        existing = _read_json(ACCOUNTS_FILE, [])
        if not isinstance(existing, list):
            existing = []

        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return web.json_response({"status": "error", "error": "UID and Password required"})

            if uid in bot_state.account_workers:
                try:
                    bot_state.account_workers[uid].cancel()
                except Exception:
                    pass
                bot_state.account_workers.pop(uid, None)

            existing = [acc for acc in existing if str(acc.get("uid", "")) != uid]
            entry = {"uid": uid, "password": pwd}
            if owner_email:
                entry["owner"] = owner_email
            existing.append(entry)

            if owner_email:
                bot_state.set_owner(uid, owner_email)
                bot_state.set_alias(uid, uid)

            identifier = uid

        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token required"})

            tok_key = token[:16]
            for k in list(bot_state.account_workers.keys()):
                if k == tok_key or k.startswith(tok_key[:10]):
                    try:
                        bot_state.account_workers[k].cancel()
                    except Exception:
                        pass
                    bot_state.account_workers.pop(k, None)

            existing = [acc for acc in existing if acc.get("token", "") != token]
            entry = {"token": token}
            if owner_email:
                entry["owner"] = owner_email
            existing.append(entry)

            if owner_email:
                bot_state.set_owner(tok_key, owner_email)
                bot_state.set_alias(tok_key, tok_key)

            identifier = f"Token_{token[:8]}..."
        else:
            return web.json_response({"status": "error", "error": "Invalid payload"})

        _write_json(ACCOUNTS_FILE, existing)
        bot_state.log(f"New account added: {identifier} (owner: {owner_email or 'none'})", "success")

        cb = bot_state.refresh_callbacks.get("on_account_added")
        if cb:
            try:
                asyncio.create_task(cb(data))
            except Exception as e:
                bot_state.log(f"on_account_added callback error: {e}", "error")

        return web.json_response({"status": "ok", "identifier": identifier, "owner": owner_email})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_delete_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        req_uid = str(data.get("uid", "")).strip()
        req_auth_uid = str(data.get("auth_uid", "")).strip()
        if not req_uid and not req_auth_uid:
            return web.json_response({"status": "error", "error": "UID is required"})

        candidate_ids: Set[str] = set()
        if req_uid:
            candidate_ids.add(req_uid)
        if req_auth_uid:
            candidate_ids.add(req_auth_uid)

        for cid in list(candidate_ids):
            if cid in bot_state.game_to_auth_id:
                candidate_ids.add(str(bot_state.game_to_auth_id[cid]))
            if cid in bot_state.auth_to_game_id:
                candidate_ids.add(str(bot_state.auth_to_game_id[cid]))

        target_tokens: Set[str] = set()
        for cid in list(candidate_ids):
            acc_info = bot_state.accounts.get(cid, {})
            if acc_info:
                if acc_info.get("auth_uid"):
                    candidate_ids.add(str(acc_info["auth_uid"]))
                if acc_info.get("uid"):
                    candidate_ids.add(str(acc_info["uid"]))
                t = acc_info.get("token") or acc_info.get("access_token")
                if t:
                    target_tokens.add(str(t))

        # Clean accounts.json
        if os.path.exists(ACCOUNTS_FILE):
            try:
                existing = _read_json(ACCOUNTS_FILE, [])
                if not isinstance(existing, list):
                    existing = []
                new_existing = []
                for acc in existing:
                    acc_uid = str(acc.get("uid", "")).strip()
                    acc_tok = str(acc.get("token", "")).strip()
                    is_match = False
                    if acc_uid and acc_uid in candidate_ids:
                        is_match = True
                    if acc_tok and (acc_tok in candidate_ids or acc_tok in target_tokens):
                        is_match = True
                    if not is_match:
                        new_existing.append(acc)
                _write_json(ACCOUNTS_FILE, new_existing)
            except Exception:
                pass

        # Clean in-memory
        for cid in candidate_ids:
            bot_state.accounts.pop(cid, None)
            bot_state.account_credentials.pop(cid, None)
            bot_state.auth_to_game_id.pop(cid, None)
            bot_state.game_to_auth_id.pop(cid, None)
            bot_state.account_token_map.pop(cid, None)
            bot_state.paused_accounts.discard(cid)
            bot_state.owners.pop(cid, None)
            bot_state.uid_aliases.pop(cid, None)

        bot_state.save_owners_to_disk()
        bot_state.save_aliases_to_disk()

        cancelled_keys = []
        for k, worker in list(bot_state.account_workers.items()):
            k_str = str(k)
            if k_str in candidate_ids:
                try:
                    worker.cancel()
                except Exception:
                    pass
                cancelled_keys.append(k)

        for k in cancelled_keys:
            bot_state.account_workers.pop(k, None)

        for cid in candidate_ids:
            bot_state.close_writers_for_account(cid)

        cb = bot_state.refresh_callbacks.get("on_account_deleted")
        if cb:
            try:
                asyncio.create_task(cb(list(candidate_ids)))
            except Exception:
                pass

        bot_state.log(f"Account {req_uid or req_auth_uid} deleted.", "warning")
        bot_state.recalc_totals()
        return web.json_response({"status": "ok", "deleted": list(candidate_ids)})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_refresh_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        cb = bot_state.refresh_callbacks.get("on_refresh_account")
        if cb:
            try:
                asyncio.create_task(cb(uid))
            except Exception as e:
                bot_state.log(f"refresh callback error: {e}", "error")
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_restart_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        cb = bot_state.refresh_callbacks.get("on_restart_account")
        if cb:
            try:
                asyncio.create_task(cb(uid))
            except Exception as e:
                bot_state.log(f"restart callback error: {e}", "error")
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_clear_logs(request: web.Request) -> web.Response:
    bot_state.logs.clear()
    return web.json_response({"status": "ok"})


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


# ==================== SERVER START ====================
async def start_web_dashboard(host: str = "0.0.0.0", port: int = 5000):
    bot_state.load_owners_from_disk()
    bot_state.load_aliases_from_disk()

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/stats", handle_get_stats)

    # Auth
    app.router.add_post("/api/auth/login", handle_login)

    # Admin credentials
    app.router.add_post("/api/admin/save-credential", handle_save_credential)
    app.router.add_get("/api/admin/list-credentials", handle_list_credentials)
    app.router.add_post("/api/admin/credential-action", handle_credential_action)

    # Account management
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/remove", handle_delete_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/restart", handle_restart_account)
    app.router.add_post("/api/account/stop", handle_toggle_pause)
    app.router.add_post("/api/account/pause", handle_toggle_pause)
    app.router.add_post("/api/account/pause_all", handle_toggle_pause_all)
    app.router.add_post("/api/logs/clear", handle_clear_logs)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    print(f"\033[92m[+] Dashboard live at http://{host}:{port}\033[0m", flush=True)
    print(f"\033[92m[+] Owners loaded: {len(bot_state.owners)}\033[0m", flush=True)
    print(f"\033[92m[+] Aliases loaded: {len(bot_state.uid_aliases)}\033[0m", flush=True)
    return runner


if __name__ == "__main__":
    async def _test_main():
        print("[TEST] dashboard_server.py standalone...", flush=True)
        runner = await start_web_dashboard(host="0.0.0.0", port=20331)
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await runner.cleanup()

    asyncio.run(_test_main())