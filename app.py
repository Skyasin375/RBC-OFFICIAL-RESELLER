# ==================== STANDARD IMPORTS ====================
import sys
import asyncio
import httpx
import random
import json
import socket
import struct
import time
import os
import uuid
import itertools
import base64
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ==================== ORIGINAL IMPORTS ====================
from google_play_scraper import app as play_scraper
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from protobuf_decoder.protobuf_decoder import Parser
from message_ids import MESSAGE_ID_TO_NAME
import thunderFF_pb2

# ==================== WEB DASHBOARD ====================
from dashboard_server import bot_state, start_web_dashboard

# ==================== CONFIGURATION (Render-ready) ====================
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "31178")))

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://rbclevelofficial.onrender.com").rstrip("/")

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")

DATA_DIR = os.getenv("DATA_DIR", ".")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
TOKEN_CACHE_FILE = os.path.join(DATA_DIR, "token_cache.json")
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")
TOKEN_CACHE_TTL = 1200

START_MATCH_INTERVAL = 3.0
NEW_MATCH_DELAY = 3.0
MAX_MATCH_DURATION = 700
MATCH_IDLE_TIMEOUT = 8.0
PRIORITY_REGIONS = ["BD", "IND", "SG", "TH", "PH", "VN", "MY", "ID", "HK", "TW"]

MAX_CONSECUTIVE_PARSE_FAILURES = 5.0
NON_MATCH_RECONNECT_DELAY = 1.0

PROFILE_REFRESH_INTERVAL = 45.0

FALLBACK_UID = ""
FALLBACK_PASSWORD = ""

# ==================== PUBLIC IP CACHE ====================
_PUBLIC_IP_CACHE = None

async def get_public_ip():
    global _PUBLIC_IP_CACHE
    if _PUBLIC_IP_CACHE:
        return _PUBLIC_IP_CACHE
    try:
        r = await client.get("https://api.ipify.org", timeout=5)
        _PUBLIC_IP_CACHE = r.text.strip()
        print_info(f"[PUBLIC_IP] Detected egress IP: {_PUBLIC_IP_CACHE}")
    except Exception as e:
        print_warning(f"[PUBLIC_IP] Could not fetch: {e} — using 0.0.0.0")
        _PUBLIC_IP_CACHE = "0.0.0.0"
    return _PUBLIC_IP_CACHE


# ==================== VALID OB55 DEVICE PROFILES ====================
DEVICE_PROFILES = [
    {
        "brand": "samsung",
        "model": "SM-G998B",
        "gpu_renderer": "Mali-G78",
        "system_software": "Android OS 13 / API-33",
        "screen_width": 1440,
        "screen_height": 3088,
        "screen_dpi": "560",
        "memory": 11500,
        "processor_details": "ARM64 FP ASIMD AES VMH | 2800 | 8",
        "astc_bitset": 4095,
    },
    {
        "brand": "Xiaomi",
        "model": "2201122G",
        "gpu_renderer": "Adreno (TM) 730",
        "system_software": "Android OS 13 / API-33",
        "screen_width": 1440,
        "screen_height": 3200,
        "screen_dpi": "520",
        "memory": 12000,
        "processor_details": "ARM64 FP ASIMD AES VMH | 3000 | 8",
        "astc_bitset": 4095,
    },
    {
        "brand": "OnePlus",
        "model": "CPH2451",
        "gpu_renderer": "Adreno (TM) 740",
        "system_software": "Android OS 14 / API-34",
        "screen_width": 1240,
        "screen_height": 2772,
        "screen_dpi": "450",
        "memory": 16000,
        "processor_details": "ARM64 FP ASIMD AES VMH | 3200 | 8",
        "astc_bitset": 4095,
    },
    {
        "brand": "realme",
        "model": "RMX3700",
        "gpu_renderer": "Mali-G710",
        "system_software": "Android OS 13 / API-33",
        "screen_width": 1080,
        "screen_height": 2412,
        "screen_dpi": "400",
        "memory": 8000,
        "processor_details": "ARM64 FP ASIMD AES VMH | 2600 | 8",
        "astc_bitset": 4095,
    },
]


def get_device_for_account(account_identifier: str) -> dict:
    devices = {}
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                devices = json.load(f)
        except Exception:
            pass

    acc_key = str(account_identifier)
    if acc_key in devices:
        return devices[acc_key]

    profile = dict(random.choice(DEVICE_PROFILES))
    profile["unique_device_id"] = f"Google|{uuid.uuid4()}"
    profile["client_ip"] = "0.0.0.0"
    devices[acc_key] = profile

    try:
        with open(DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(devices, f, indent=4)
    except Exception as e:
        print_error(f"Failed to save device mapping: {e}")

    return profile


# ==================== CLOUDFLARE DNS RESOLVER ====================
CLOUDFLARE_PRIMARY_DNS = "1.1.1.1"
CLOUDFLARE_SECONDARY_DNS = "1.0.0.1"
_DNS_CACHE: Dict[str, Tuple[str, float]] = {}
_DNS_CACHE_TTL = 300.0


async def resolve_host_cloudflare(hostname: str) -> str:
    if not hostname:
        return hostname

    parts = hostname.split('.')
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return hostname

    now = time.time()
    if hostname in _DNS_CACHE:
        ip, exp = _DNS_CACHE[hostname]
        if now < exp:
            return ip

    def _query_cloudflare(server_ip: str) -> Optional[str]:
        s = None
        try:
            tx_id = random.randint(1000, 65535)
            header = struct.pack(">HHHHHH", tx_id, 0x0100, 1, 0, 0, 0)
            qname = b"".join(bytes([len(part)]) + part.encode('ascii') for part in hostname.split('.')) + b"\x00"
            query_pkt = header + qname + struct.pack(">HH", 1, 1)

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.2)
            s.sendto(query_pkt, (server_ip, 53))
            resp, _ = s.recvfrom(1024)

            if len(resp) >= 12:
                ancount = struct.unpack(">H", resp[6:8])[0]
                if ancount > 0:
                    offset = 12 + len(qname) + 4
                    for _ in range(ancount):
                        if offset >= len(resp):
                            break
                        if (resp[offset] & 0xC0) == 0xC0:
                            offset += 2
                        else:
                            while offset < len(resp) and resp[offset] != 0:
                                offset += 1 + resp[offset]
                            offset += 1
                        if offset + 10 > len(resp):
                            break
                        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", resp[offset:offset+10])
                        offset += 10
                        if rtype == 1 and rdlen == 4 and offset + 4 <= len(resp):
                            return socket.inet_ntoa(resp[offset:offset+4])
                        offset += rdlen
        except Exception:
            pass
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        return None

    loop = asyncio.get_running_loop()
    ip = await loop.run_in_executor(None, _query_cloudflare, CLOUDFLARE_PRIMARY_DNS)
    if not ip:
        ip = await loop.run_in_executor(None, _query_cloudflare, CLOUDFLARE_SECONDARY_DNS)
    if not ip:
        try:
            ip_info = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
            if ip_info:
                ip = ip_info[0][4][0]
        except Exception:
            ip = hostname

    if ip:
        _DNS_CACHE[hostname] = (ip, now + _DNS_CACHE_TTL)
    return ip or hostname


def optimize_tcp_socket(sock: socket.socket):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    except Exception:
        pass


def optimize_udp_socket(sock: socket.socket):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
        if hasattr(socket, 'SIO_UDP_CONNRESET') and os.name == 'nt':
            try:
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except Exception:
                pass
    except Exception:
        pass


# ==================== NETWORK & CRYPTO ====================
client = httpx.AsyncClient(
    verify=False,
    timeout=10.0,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=50)
)

headers = {
    'User-Agent': 'UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
    'Connection': 'Keep-Alive',
    'Accept-Encoding': 'gzip',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Expect': '100-continue',
    'X-Unity-Version': '2018.4.12f1',
    'X-GA-SV': '1789535859',
    'X-GA': 'v1 1',
    'ReleaseVersion': 'OB55'
}

AES_KEY = b'Yg&tc%DEuh6%Zc^8'
AES_IV = b'6oyZDr22E3ychjM%'

CRC7_TABLE = bytes([
    0, 9, 18, 27, 36, 45, 54, 63, 72, 65, 90, 83, 108, 101, 126, 119,
    25, 16, 11, 2, 61, 52, 47, 38, 81, 88, 67, 74, 117, 124, 103, 110,
    50, 59, 32, 41, 22, 31, 4, 13, 122, 115, 104, 97, 94, 87, 76, 69,
    43, 34, 57, 48, 15, 6, 29, 20, 99, 106, 113, 120, 71, 78, 85, 92,
    100, 109, 118, 127, 64, 73, 82, 91, 44, 37, 62, 55, 8, 1, 26, 19,
    125, 116, 111, 102, 89, 80, 75, 66, 53, 60, 39, 46, 17, 24, 3, 10,
    86, 95, 68, 77, 114, 123, 96, 105, 30, 23, 12, 5, 58, 51, 40, 33,
    79, 70, 93, 84, 107, 98, 121, 112, 7, 14, 21, 28, 35, 42, 49, 56,
    65, 72, 83, 90, 101, 108, 119, 126, 9, 0, 27, 18, 45, 36, 63, 54,
    88, 81, 74, 67, 124, 117, 110, 103, 16, 25, 2, 11, 52, 61, 38, 47,
    115, 122, 97, 104, 87, 94, 69, 76, 59, 50, 41, 32, 31, 22, 13, 4,
    106, 99, 120, 113, 78, 71, 92, 85, 34, 43, 48, 57, 6, 15, 20, 29,
    37, 44, 55, 62, 1, 8, 19, 26, 109, 100, 127, 118, 73, 64, 91, 82,
    60, 53, 46, 39, 24, 17, 10, 3, 116, 125, 102, 111, 80, 89, 66, 75,
    23, 30, 5, 12, 51, 58, 33, 40, 95, 86, 77, 68, 123, 114, 105, 96,
    14, 7, 28, 21, 42, 35, 56, 49, 70, 79, 84, 93, 98, 107, 112, 121,
])

_DELTA = 0x9E3779B9
_ROUNDS = 16
_FIELD_SIZES = {0: 1, 1: 2, 2: 2, 3: 1, 4: 2}
_FIELD_NAMES = {0: "sendOption", 1: "cmd", 2: "orderId", 3: "flags", 4: "length"}


class Colors:
    HEADER = '\033[95m'
    GREEN = '\033[92m'
    FAIL = '\033[91m'
    WARNING = '\033[93m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    WHITE = '\033[97m'
    ENDC = '\033[0m'


def print_colored(text, color=Colors.WHITE):
    try:
        print(f"{color}{text}{Colors.ENDC}")
    except Exception:
        try:
            print(f"{color}{text.encode('ascii', errors='replace').decode('ascii')}{Colors.ENDC}")
        except Exception:
            pass


def print_success(text):
    print_colored(f"[+] {text}", Colors.GREEN)
    try:
        bot_state.log(text, "success")
    except Exception:
        pass


def print_error(text):
    print_colored(f"[-] {text}", Colors.FAIL)
    try:
        bot_state.log(text, "error")
    except Exception:
        pass


def print_warning(text):
    print_colored(f"[!] {text}", Colors.WARNING)
    try:
        bot_state.log(text, "warning")
    except Exception:
        pass


def print_info(text):
    print_colored(f"[i] {text}", Colors.CYAN)
    try:
        bot_state.log(text, "info")
    except Exception:
        pass


def get_proto_field(d, key, default=None):
    if not d or not isinstance(d, dict):
        return default
    if key in d:
        val = d[key].get('data')
        return val if val is not None else default
    if str(key) in d:
        val = d[str(key)].get('data')
        return val if val is not None else default
    return default


# ==================== PER-ACCOUNT MATCH COUNTER ====================
_match_counters: Dict[str, int] = {}
_match_counter_lock = asyncio.Lock()


async def _inc_match(uid: str) -> int:
    async with _match_counter_lock:
        _match_counters[uid] = _match_counters.get(uid, 0) + 1
        return _match_counters[uid]


async def _dec_match(uid: str) -> int:
    async with _match_counter_lock:
        if uid in _match_counters and _match_counters[uid] > 0:
            _match_counters[uid] -= 1
        return _match_counters.get(uid, 0)


async def _get_match_count(uid: str) -> int:
    async with _match_counter_lock:
        return _match_counters.get(uid, 0)


async def _get_total_match_count() -> int:
    async with _match_counter_lock:
        return sum(_match_counters.values())


# ==================== TOKEN CACHE ====================
_token_cache_memo: Dict[str, Any] = {}
_token_cache_memo_time: float = 0.0
_TOKEN_CACHE_MEMO_TTL = 5.0


def _json_serializer(obj):
    if isinstance(obj, (bytes, bytearray)):
        return {"__bytes_hex__": bytes(obj).hex()}
    raise TypeError(f"Type {type(obj)} not serializable")


def _json_deserializer(obj):
    if isinstance(obj, dict):
        if "__bytes_hex__" in obj and len(obj) == 1:
            try:
                return bytes.fromhex(obj["__bytes_hex__"])
            except Exception:
                return b""
        return {k: _json_deserializer(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_deserializer(x) for x in obj]
    return obj


def _load_token_cache() -> Dict[str, Any]:
    global _token_cache_memo, _token_cache_memo_time
    now = time.time()
    if _token_cache_memo and (now - _token_cache_memo_time) < _TOKEN_CACHE_MEMO_TTL:
        return _token_cache_memo

    if not os.path.exists(TOKEN_CACHE_FILE):
        return {}
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return {}
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("Cache root must be dict")
        parsed = _json_deserializer(data)
        _token_cache_memo = parsed
        _token_cache_memo_time = now
        return parsed
    except Exception as e:
        print_error(f"Token cache corrupt → deleting: {e}")
        try:
            os.remove(TOKEN_CACHE_FILE)
        except Exception:
            pass
        return {}


def _save_token_cache(cache: Dict[str, Any]):
    global _token_cache_memo, _token_cache_memo_time
    try:
        tmp_file = TOKEN_CACHE_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, default=_json_serializer)
        os.replace(tmp_file, TOKEN_CACHE_FILE)
        _token_cache_memo = cache
        _token_cache_memo_time = time.time()
    except Exception as e:
        print_error(f"Token cache save error: {e}")


def cache_get(uid: str) -> Optional[Dict]:
    cache = _load_token_cache()
    entry = cache.get(str(uid))
    if not entry:
        return None
    if time.time() - entry.get("cached_at", 0) > TOKEN_CACHE_TTL:
        print_info(f"[CACHE] UID {uid} expired. Re-login needed.")
        cache_invalidate(uid)
        return None
    if str(entry.get("account_id", "")).isdigit():
        entry["account_id"] = int(entry["account_id"])
    if not isinstance(entry.get("login_payload_data"), (bytes, bytearray)):
        print_warning(f"[CACHE] UID {uid} missing payload → invalidating")
        cache_invalidate(uid)
        return None
    return entry


def cache_set(uid: str, account_data: Dict):
    cache = _load_token_cache()
    entry = dict(account_data)
    entry["cached_at"] = time.time()
    cache[str(uid)] = entry
    _save_token_cache(cache)
    print_success(f"[CACHE] Saved credentials for UID {uid}")


def cache_invalidate(uid: str):
    cache = _load_token_cache()
    if str(uid) in cache:
        del cache[str(uid)]
        _save_token_cache(cache)
        print_warning(f"[CACHE] Invalidated: {uid}")


# ==================== ENCRYPTION & PROTOBUF ====================

async def aes_encrypt(payload, key, iv):
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pad(payload, AES.block_size))


async def get_playstore_version():
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: play_scraper('com.dts.freefireth', lang='hi', country='id')
    )
    return result.get("version")


async def version_config():
    app_version = await get_playstore_version()
    print_info(f"[VERSION_CFG] Play Store version = {app_version}")
    api_url = (
        "https://version.ggwhitehawk.com/live/ver.php"
        f"?version={app_version}"
        "&lang=hi&device=android&channel=android"
        "&appstore=googleplay&region=BD"
        "&whitelist_version=1.3.0&whitelist_sp_version=1.0.0"
    )
    try:
        response = await client.get(api_url)
        print_info(f"[VERSION_CFG] HTTP {response.status_code} | URL: {api_url}")
        if response.status_code != 200:
            print_error(f"[VERSION_CFG] Non-200 response body: {response.text[:500]}")
            return None
        data = response.json()
        print_info(f"[VERSION_CFG] RAW JSON: {json.dumps(data)[:600]}")
        server_url = data.get("server_url")
        remote_version = data.get("remote_version")
        latest_release_version = data.get("latest_release_version")
        if not server_url or not remote_version or not latest_release_version:
            print_error(f"[VERSION_CFG] Missing fields → server_url={server_url} | remote_version={remote_version} | latest={latest_release_version}")
            return None
        print_success(f"[VERSION_CFG] OK → release={latest_release_version} | remote={remote_version} | server={server_url}")
        return latest_release_version, remote_version, server_url
    except Exception as e:
        import traceback
        print_error(f"[VERSION_CFG] EXCEPTION: {e}")
        traceback.print_exc()
        return None


async def get_access_token(uid, password):
    url = "https://100067.connect.garena.com/oauth/guest/token/grant"
    hdrs = {
        "Host": "100067.connect.garena.com",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; SM-G998B Build/SP1A.210812.016)",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "close"
    }
    data = {
        "uid": uid,
        "password": password,
        "response_type": "token",
        "client_type": "2",
        "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
        "client_id": "100067"
    }
    for attempt in range(5):
        try:
            response = await client.post(url, headers=hdrs, data=data)
            print_info(f"[OAUTH] Attempt {attempt+1}/5 → HTTP {response.status_code}")
            body = response.text
            print_info(f"[OAUTH] RAW BODY: {body[:600]}")
            if response.status_code == 200:
                try:
                    response_data = response.json()
                except Exception as je:
                    print_error(f"[OAUTH] JSON decode failed: {je}")
                    await asyncio.sleep(0.5)
                    continue
                open_id = response_data.get("open_id")
                access_token = response_data.get("access_token")
                platform = response_data.get("platform", 4)
                if open_id and access_token:
                    print_success(f"[OAUTH] OK → open_id={open_id[:12]}... | platform={platform}")
                    return open_id, access_token, platform
                print_error(f"[OAUTH] Missing tokens: {response_data}")
            elif response.status_code == 429:
                print_warning("[OAUTH] Rate limited (429), retrying...")
                await asyncio.sleep(1)
                continue
            else:
                print_error(f"[OAUTH] Non-200 → {response.status_code} | {body[:500]}")
        except Exception as e:
            import traceback
            print_error(f"[OAUTH] EXCEPTION: {e}")
            traceback.print_exc()
        await asyncio.sleep(0.5)
    return None


async def parse_results(parsed_results):
    result_dict = {}
    for result in parsed_results:
        field_data = {"wire_type": result.wire_type}
        if result.wire_type == "varint":
            field_data["data"] = result.data
        elif result.wire_type == "string":
            field_data["data"] = result.data
        elif result.wire_type == "bytes":
            field_data["data"] = result.data
        elif result.wire_type == "length_delimited":
            field_data["data"] = await parse_results(result.data.results)
        result_dict[result.field] = field_data
    return result_dict


async def decode_protobuf(data):
    parsed_results = Parser().parse(data)
    parsed_results_dict = await parse_results(parsed_results)
    return json.dumps(parsed_results_dict)


# ==================== PLAYER PROFILE FETCH (FIXED) ====================
REGION_BASE_MAP = {
    "BD": "https://clientbp.common.ggbluefox.com",
    "SG": "https://clientbp.common.ggbluefox.com",
    "MY": "https://clientbp.common.ggbluefox.com",
    "IND": "https://client.ind.freefiremobile.com",
    "PH": "https://client.ind.freefiremobile.com",
    "VN": "https://client.ind.freefiremobile.com",
    "ID": "https://client.ind.freefiremobile.com",
}


def _region_base(region: str) -> str:
    return REGION_BASE_MAP.get(str(region or "BD").upper(), REGION_BASE_MAP["BD"])


async def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


# Level minimum EXP estimates (fallback agar server exp=0 bhej de)
LEVELS_MIN_EXP = {
    1: 0, 2: 48, 3: 202, 4: 544, 5: 1012, 6: 1844, 7: 2792, 8: 3800, 9: 4870, 10: 6004,
    11: 7192, 12: 8448, 13: 9776, 14: 11140, 15: 12566, 16: 14060, 17: 15610, 18: 17224,
    19: 18902, 20: 20632, 21: 22424, 22: 24728, 23: 26192, 24: 28166, 25: 30200,
    26: 32294, 27: 34448, 28: 37804, 29: 41174, 30: 44870, 31: 48852, 32: 53334,
    33: 58566, 34: 64096, 35: 69994, 36: 76460, 37: 83108, 38: 91128, 39: 99322,
    40: 108092, 41: 120144, 42: 133266, 43: 147472, 44: 162760, 45: 179126,
    46: 196572, 47: 215368, 48: 235516, 49: 257010, 50: 279860, 51: 304056,
    52: 348318, 53: 394982, 54: 444044, 55: 495508, 56: 549364, 57: 633756,
    58: 721744, 59: 813336, 60: 908522, 61: 1041438, 62: 1180352, 63: 1325256,
    64: 1476184, 65: 1634300, 66: 1840946, 67: 2056594, 68: 2281242, 69: 2514880,
    70: 2757530, 71: 3059506, 72: 3372284, 73: 3699456, 74: 4041030, 75: 4397020,
    76: 4829104, 77: 5282204, 78: 5756304, 79: 6251404, 80: 6767504, 81: 7381324,
    82: 8043154, 83: 8752952, 84: 9510808, 85: 10316638, 86: 11277190, 87: 12360748,
    88: 13360304, 89: 14482858, 90: 15659418, 91: 17026708, 92: 18453688,
    93: 19941280, 94: 21488570, 95: 23095858, 96: 24763138, 97: 26490138,
    98: 28277708, 99: 30124996, 100: 32032284,
}


def _min_exp_for_level(level: int) -> int:
    """Level se minimum EXP estimate karo."""
    try:
        lv = int(level)
    except (TypeError, ValueError):
        return 0
    if lv <= 1:
        return 0
    best = 0
    for k in sorted(LEVELS_MIN_EXP.keys()):
        if k <= lv:
            best = LEVELS_MIN_EXP[k]
        else:
            break
    return best


async def fetch_player_profile(uid, region, bearer_token):
    """
    Fetch real player profile (nickname, level, exp, likes, region).
    FIX: 3x retry, X-GA header, exp=0 fallback, debug logging.
    """
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        print_error(f"[PROFILE] invalid uid: {uid!r}")
        return None

    if not bearer_token:
        print_error(f"[PROFILE] missing bearer token for uid {uid}")
        return None

    base = _region_base(region)
    url = f"{base}/GetPlayerPersonalShow"

    inner = b"\x08" + await _encode_varint(uid_int) + b"\x10\x07"
    body = await aes_encrypt(inner, AES_KEY, AES_IV)

    req_headers = {
        "X-Unity-Version": "2018.4.12f1",
        "ReleaseVersion": "OB55",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-GA": "v1 1",
        "Authorization": f"Bearer {bearer_token}",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 7.1.2; ASUS_Z01QD Build/QKQ1.190825.002)",
        "Host": base.split("://", 1)[-1],
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
    }

    # ✅ 3 attempts
    resp = None
    last_err = None
    for attempt in range(3):
        try:
            resp = await client.post(url, headers=req_headers, data=body)
            if resp.status_code in (200, 201):
                break
            print_warning(f"[PROFILE] attempt {attempt+1} HTTP {resp.status_code} for uid={uid}")
        except Exception as e:
            last_err = e
            print_warning(f"[PROFILE] attempt {attempt+1} failed uid={uid}: {e}")
        await asyncio.sleep(1.5)

    if not resp or resp.status_code not in (200, 201):
        print_error(f"[PROFILE] all attempts failed for uid={uid} | err={last_err}")
        return None

    try:
        hex_packet = resp.content.hex()
        decoded = json.loads(await decode_protobuf(hex_packet))
    except Exception as e:
        print_error(f"[PROFILE] decode failed uid={uid}: {e}")
        return None

    # ✅ Debug — Render logs me dikhega
    try:
        print_info(f"[PROFILE-DEBUG] uid={uid} keys={list(decoded.keys())[:20] if isinstance(decoded, dict) else 'N/A'}")
        print_info(f"[PROFILE-DEBUG] uid={uid} decoded={json.dumps(decoded)[:500]}")
    except Exception:
        pass

    wrapper = decoded.get("1", {}).get("data", {}) if isinstance(decoded, dict) else {}
    if not isinstance(wrapper, dict):
        wrapper = {}

    def _pick(field_key, fallback=None):
        node = wrapper.get(str(field_key))
        if isinstance(node, dict) and "data" in node:
            return node["data"]
        node = decoded.get(str(field_key)) if isinstance(decoded, dict) else None
        if isinstance(node, dict) and "data" in node:
            return node["data"]
        return fallback

    try:
        nickname = _pick(3, "") or ""
        level = int(_pick(6, 1) or 1)
        exp = int(_pick(7, 0) or 0)
        likes = int(_pick(21, 0) or 0)
        player_region = str(_pick(5, region or "BD") or region or "BD").upper()
    except (TypeError, ValueError) as e:
        print_error(f"[PROFILE] parse error uid={uid}: {e}")
        return None

    if level <= 0:
        level = 1
    if exp < 0:
        exp = 0
    if likes < 0:
        likes = 0

    # ✅ FIX: Agar exp=0 but level>1, to level se minimum exp estimate karo
    if exp == 0 and level > 1:
        est = _min_exp_for_level(level)
        if est > 0:
            exp = est
            print_warning(f"[PROFILE] UID {uid} exp=0 → estimated {exp} from level {level}")

    print_success(
        f"[PROFILE] UID {uid} → nickname={nickname!r} | level={level} | "
        f"exp={exp} | likes={likes} | region={player_region}"
    )
    return {
        "uid": str(uid),
        "nickname": nickname,
        "level": level,
        "exp": exp,
        "likes": likes,
        "region": player_region,
    }


# ==================== SAFE PROTO SETTER ====================
def _safe_set(proto, field_name, value):
    try:
        setattr(proto, field_name, value)
        return True
    except (AttributeError, ValueError, TypeError):
        return False


# ==================== MAJORLOGIN PAYLOAD ====================
async def build_majorlogin_payload(open_id, access_token, platform, client_version, device_info):
    try:
        proto = thunderFF_pb2.MajorLoginReq()
        proto.event_time = str(datetime.now())[:-7]
        proto.game_name = "free fire"

        proto.platform_id = int(platform)

        VERSION_CODE_MAP = {
            "1.132.8": "2024112901",
            "1.132.6": "2024111401",
            "1.132.1": "2024110101",
        }
        proto.client_version = client_version
        proto.client_version_code = VERSION_CODE_MAP.get(client_version, "2024112901")

        build_id = f"TP1A.220624.014/{uuid.uuid4().hex[:16]}"
        base_sw = device_info.get("system_software", "Android OS 13 / API-33")
        proto.system_software = f"{base_sw} ({build_id})"
        proto.system_hardware = device_info.get("brand", "samsung")
        proto.device_type = device_info.get("model", "SM-G998B")
        proto.screen_width = int(device_info.get("screen_width", 1440))
        proto.screen_height = int(device_info.get("screen_height", 3088))
        proto.screen_dpi = str(device_info.get("screen_dpi", "560"))
        proto.processor_details = device_info.get("processor_details", "ARM64 FP ASIMD AES VMH | 2800 | 8")
        proto.memory = int(device_info.get("memory", 11500))
        proto.gpu_renderer = device_info.get("gpu_renderer", "Mali-G78")
        proto.unique_device_id = device_info.get("unique_device_id", f"Google|{uuid.uuid4()}")

        real_ip = await get_public_ip()
        proto.client_ip = real_ip if real_ip != "0.0.0.0" else device_info.get("client_ip", "103.145.112.210")

        proto.telecom_operator = "Citycell"
        proto.network_operator_a = "Citycell"
        proto.network_type = "WIFI"
        proto.network_type_a = "WIFI"
        proto.cpu_type = 2
        proto.cpu_architecture = "64"
        proto.gpu_version = "OpenGL ES 3.2"
        proto.graphics_api = "OpenGLES2"
        proto.language = "en"
        proto.open_id = open_id
        proto.open_id_type = str(platform)
        proto.login_open_id_type = int(platform)
        proto.access_token = access_token
        proto.login_by = 3
        proto.platform_sdk_id = 2
        proto.origin_platform_type = str(platform)
        proto.primary_platform_type = str(platform)
        proto.reg_avatar = 1
        proto.channel_type = 3

        try:
            proto.appstore = "googleplay"
        except (AttributeError, ValueError, TypeError):
            pass

        memory_available = proto.memory_available
        memory_available.version = 55
        memory_available.hidden_value = 81

        proto.external_storage_total = 34308
        proto.external_storage_available = 30777
        proto.internal_storage_total = 2519
        proto.internal_storage_available = 243
        proto.game_disk_storage_total = 34308
        proto.game_disk_storage_available = 32224
        proto.external_sdcard_total_storage = 34308
        proto.external_sdcard_avail_storage = 32224

        seg = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=") + "=="
        pkg_hash = "".join(random.choices("abcdef0123456789", k=32))
        proto.library_path = f"/data/app/~~{seg}/com.dts.freefireth-{pkg_hash}==/lib/arm64"
        proto.library_token = f"{seg}|/data/app/~~{seg}/com.dts.freefireth-{pkg_hash}==/base.apk"

        proto.client_using_version = "7428b253defc164018c604a1ebbfebdf"
        proto.supported_astc_bitset = int(device_info.get("astc_bitset", 4095))

        proto.loading_time = random.randint(8000, 25000)
        proto.release_channel = "android"
        proto.android_engine_init_flag = 111207
        proto.if_push = 1
        proto.is_vpn = 0

        try:
            proto.analytics_detail = b""
        except (AttributeError, ValueError, TypeError):
            pass
        try:
            proto.extra_info = ""
        except (AttributeError, ValueError, TypeError):
            pass

        payload = proto.SerializeToString()
        print_info(f"[MAJORLOGIN-BUILD] payload={len(payload)}B | ver={client_version}/{proto.client_version_code} | ip={proto.client_ip} | device={proto.device_type}/{proto.gpu_renderer}")
        return await aes_encrypt(payload, AES_KEY, AES_IV)
    except Exception as e:
        import traceback
        print_error(f"[MAJORLOGIN-BUILD] {e}")
        traceback.print_exc()
        return None


async def send_majorlogin(data, release_version, server_url):
    try:
        url = f"{server_url}MajorLogin"
        req_headers = headers.copy()
        req_headers["ReleaseVersion"] = release_version
        print_info(f"[MAJORLOGIN] POST → {url}")
        response = await client.post(url, headers=req_headers, data=data)
        print_info(f"[MAJORLOGIN] HTTP {response.status_code} | resp_len={len(response.content)} bytes")
        if response.status_code != 200:
            print_error(f"[MAJORLOGIN] Non-200 → {response.status_code}")
            try:
                print_error(f"[MAJORLOGIN] BODY: {response.text[:800]}")
            except Exception:
                print_error(f"[MAJORLOGIN] HEX: {response.content[:200].hex()}")
            return None

        response_content = response.content
        print_info(f"[MAJORLOGIN] FULL HEX ({len(response_content)}): {response_content.hex()[:400]}...")

        try:
            with open("majorlogin_response.bin", "wb") as f:
                f.write(response_content)
            with open("majorlogin_request.bin", "wb") as f:
                f.write(data)
        except Exception:
            pass

        res_proto = thunderFF_pb2.MajorLoginRes()
        try:
            res_proto.ParseFromString(response_content)
            if res_proto.region and res_proto.token:
                return res_proto
        except Exception:
            pass

        if len(response_content) > 64:
            try:
                res_proto = thunderFF_pb2.MajorLoginRes()
                res_proto.ParseFromString(response_content[64:])
                if res_proto.region and res_proto.token:
                    return res_proto
            except Exception:
                pass

        for offset in range(min(128, len(response_content))):
            try:
                candidate = thunderFF_pb2.MajorLoginRes()
                candidate.ParseFromString(response_content[offset:])
                if candidate.region and candidate.token:
                    return candidate
            except Exception:
                pass

        res_proto = thunderFF_pb2.MajorLoginRes()
        res_proto.ParseFromString(response_content)
        return res_proto
    except Exception:
        return None


async def send_getlogin(data, base_url, token, release_version):
    try:
        url = f"{base_url.rstrip('/')}/GetLoginData"
        req_headers = headers.copy()
        req_headers["ReleaseVersion"] = release_version
        req_headers['Authorization'] = f"Bearer {token}"
        req_headers['Host'] = "clientbp.ppmainecoonghj.com"
        print_info(f"[GETLOGIN] POST → {url}")
        response = await client.post(url, headers=req_headers, data=data)
        print_info(f"[GETLOGIN] HTTP {response.status_code} | resp_len={len(response.content)} bytes")
        if response.status_code != 200:
            print_error(f"[GETLOGIN] Non-200 → {response.status_code}")
            try:
                print_error(f"[GETLOGIN] BODY: {response.text[:800]}")
            except Exception:
                print_error(f"[GETLOGIN] HEX: {response.content[:200].hex()}")
            return None
        response_content = response.content
        print_info(f"[GETLOGIN] FIRST 64: {response_content[:64].hex()}")

        res_proto = thunderFF_pb2.GetLoginDataRes()
        parsed_successfully = False
        try:
            res_proto.ParseFromString(response_content)
            if res_proto.functional_addrs or res_proto.informational_addrs:
                parsed_successfully = True
        except Exception:
            pass

        if not parsed_successfully:
            for offset in range(min(128, len(response_content))):
                try:
                    candidate = thunderFF_pb2.GetLoginDataRes()
                    candidate.ParseFromString(response_content[offset:])
                    if candidate.functional_addrs or candidate.informational_addrs:
                        res_proto = candidate
                        break
                except Exception:
                    pass

        dict_res = {}
        try:
            parsed = Parser().parse(response_content.hex())
            dict_res = await parse_results(parsed)
        except Exception:
            pass

        return res_proto, dict_res
    except Exception:
        return None


async def build_tcp_startup_packet(account_id, token, server_time, key, iv, region="BD", typ='OnLine'):
    uid_hex = f"{int(account_id):016x}"
    timestamp_hex = f"{int(server_time):08x}"
    encode_token = token.encode()
    encrypted_packet = (await aes_encrypt(encode_token, key, iv)).hex()
    encrypted_packet_length = f"{len(encrypted_packet) // 2:08x}"
    reg = str(region).upper() if region else "BD"
    if typ == 'OnLine':
        prefix = '7119' if reg == 'BD' else ('7114' if reg == 'IND' else '7115')
        return f"{prefix}{uid_hex}{timestamp_hex}00000000{encrypted_packet_length}{encrypted_packet}"
    else:
        prefix = '9219' if reg == 'BD' else ('9214' if reg == 'IND' else '9215')
        return f"{prefix}{uid_hex}{timestamp_hex}{encrypted_packet_length}{encrypted_packet}"


async def send_keep_alive(region="BD"):
    try:
        reg = str(region).upper() if region else "BD"
        ka_hex = "0219" if reg == "BD" else ("0214" if reg == "IND" else "0215")
        return bytes.fromhex(ka_hex)
    except Exception:
        return bytes.fromhex("0219")


async def start_game_lone_wolf(region, client_version, writer, key, iv):
    packet = bytes.fromhex("080112800a0a010b102b3a110a044944433110aa011a064555524f50453a100a044944433210311a064555524f504540014a0801090a0b1219202758016291090a8001303838463832424630324139363736373032303130313030303030303030303030303030303136303030313030313530303032323246393745454530463030303030303436373632353134303030303030303030303030303030303030303030303030303030303030303030303030303066663030303030303030636163666131366410241afb02735d5e571400024a775d45414d1a041b1c001f11010449715f4243481a001e1d071c1703004b1a4066785c524570735c51486775421b5c5a4c07504042685a63610816054e19025e75196001477c015165406370195f5547404e4550640103020f1304064863754268676c755f65576e40467e5f0a417a4701026d675d6e73670b1108495a4c6a0b78470b740065645e525a057258425f584a447d4e6759440c11044e7c596d7f4b625f7d04055a47505c4e1d6b5b4107447d7201057d7f0f14084e430457674f7e517d72015172415d027473577c4d615f79535256780911030f4d5e027a797f614165067806505d53777750475e75064257076500460817014e741e7e5078487e7a7c465e7669767153497064605a7376677773550d160148037e18675966787f4c42607a645f577e7b441b460776026b18685d0b110205490060020f70676175654674706671797f41067346677c4e06585e780f15074c57047b40517075415f6364027259674b5b0166407f7340600407770a22047a5d5c52300b3a0a167305067162727516134208312e3133302e3232480350015ae90403626253513635686e556f4e36416456324b796f566c636f477776484f624e56526c4d727073504b4f43654177616848494176795556497273743752737149734a7a786b3247525268377a2f637664626d504f6a73552f79626d38547a4c69586d2f474351696d494b53486833447955726f39515152756c34545350626d6d624b7949565937545671577059455372323646572f59624578507338514f706d317372785455736c30796a434144444d4f34616a654b615753366361496c554b4963797a494e396d52516f715277687939797257476d337a644345337a6a61436f492f5a585233656f65365a42647a64677654636b6b665733356e4d4c6a6a565072564b6433523172756174394e50514150724a5546627859696c4c5a3859707336654d5447666b6649793574666a526c314d4648706b51774c6373374439656378566c41636f374e664f6d2b30654756466c4434744478706771385533595973587645384842502f70666c767a737138316a32524f4d7857437556445442492f684735625462773166456e4249725162762b636144775147696f74554e316d4c4b77734379456f4766706746614251457645672b736a764c4c78704743334c304a5344532f74526169504354553344374e6249306547516651622f5a466f4c36455630775a324d6f583932414c572f5049752f56634663584e70596b356f7966326151416a536971486a2f363276354843644f525551303578754e6171795251625653704654303137655237675255636b4966366c6f447476342b514e4a4670766d74757077707774396a5a5974437a4b56743657726d6e36785837706658456251555434684f3758a201050803108703a201050804108103a20105080510c001a20105081d10cc01a2010408161078a20105080e10af01a201020815")
    proto = thunderFF_pb2.StartMatch()
    proto.ParseFromString(packet)
    if hasattr(proto.main, 'region_list') and len(proto.main.region_list) > 0:
        proto.main.region_list[0].region = region
        if len(proto.main.region_list) > 1:
            proto.main.region_list[1].region = region
    if hasattr(proto.main, 'client_version'):
        proto.main.client_version.remote_version = client_version
    packet = proto.SerializeToString()
    encrypted_packet = (await aes_encrypt(packet, key, iv)).hex()
    packet_length = len(encrypted_packet) // 2
    hex_length = hex(packet_length)[2:]
    hex_length = hex_length if len(hex_length) > 1 else "0" + hex_length
    final_packet = "031400" + "0" * (6 - len(hex_length)) + hex_length + encrypted_packet
    writer.write(bytes.fromhex(final_packet))
    await writer.drain()


# ==================== TEA / FRAME HELPERS ====================
async def has_ssan_zig(n):
    z = (n << 1) & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while z >= 0x80:
        out.append((z & 0x7F) | 0x80)
        z >>= 7
    out.append(z)
    return bytes(out)


async def uleb_encode(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


async def tea_enc(v0, v1, k0, k1, k2, k3):
    s = 0
    for _ in range(_ROUNDS):
        s = (s + _DELTA) & 0xFFFFFFFF
        v0 = (v0 + (((((v1 << 4) & 0xFFFFFFFF) + k0) & 0xFFFFFFFF ^
                      ((v1 + s) & 0xFFFFFFFF) ^
                      (((v1 >> 5) + k1) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        v1 = (v1 + (((((v0 << 4) & 0xFFFFFFFF) + k2) & 0xFFFFFFFF ^
                      ((v0 + s) & 0xFFFFFFFF) ^
                      (((v0 >> 5) + k3) & 0xFFFFFFFF)))) & 0xFFFFFFFF
    return v0, v1


async def tea_dec(v0, v1, k0, k1, k2, k3):
    s = (_DELTA * _ROUNDS) & 0xFFFFFFFF
    for _ in range(_ROUNDS):
        v1 = (v1 - (((((v0 << 4) & 0xFFFFFFFF) + k2) & 0xFFFFFFFF ^
                      ((v0 + s) & 0xFFFFFFFF) ^
                      (((v0 >> 5) + k3) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        v0 = (v0 - (((((v1 << 4) & 0xFFFFFFFF) + k0) & 0xFFFFFFFF ^
                      ((v1 + s) & 0xFFFFFFFF) ^
                      (((v1 >> 5) + k1) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        s = (s - _DELTA) & 0xFFFFFFFF
    return v0, v1


async def tea_cbc_encrypt(padded, key_bytes):
    k0, k1, k2, k3 = (struct.unpack_from("<I", key_bytes, o)[0] for o in (0, 4, 8, 12))
    out = bytearray(len(padded))
    prev_cipher = bytearray(8)
    prev_intermediate = bytearray(8)
    for i in range(0, len(padded), 8):
        xored = bytearray(8)
        for j in range(8):
            xored[j] = padded[i + j] ^ prev_cipher[j]
        e0, e1 = await tea_enc(
            struct.unpack_from("<I", xored, 0)[0],
            struct.unpack_from("<I", xored, 4)[0],
            k0, k1, k2, k3,
        )
        enc = bytearray(8)
        struct.pack_into("<I", enc, 0, e0)
        struct.pack_into("<I", enc, 4, e1)
        for j in range(8):
            out[i + j] = enc[j] ^ prev_intermediate[j]
        prev_cipher[:] = out[i:i + 8]
        prev_intermediate[:] = xored
    return bytes(out)


async def tea_cbc_decrypt(body, key_bytes):
    k0, k1, k2, k3 = (struct.unpack_from("<I", key_bytes, o)[0] for o in (0, 4, 8, 12))
    out = bytearray(len(body))
    prev_intermediate = bytearray(8)
    prev_cipher = bytearray(8)
    xored = bytearray(8)
    dec = bytearray(8)
    for i in range(0, len(body), 8):
        for j in range(8):
            xored[j] = body[i + j] ^ prev_intermediate[j]
        d0, d1 = await tea_dec(
            struct.unpack_from("<I", xored, 0)[0],
            struct.unpack_from("<I", xored, 4)[0],
            k0, k1, k2, k3
        )
        struct.pack_into("<I", dec, 0, d0)
        struct.pack_into("<I", dec, 4, d1)
        for j in range(8):
            out[i + j] = dec[j] ^ prev_cipher[j]
        prev_cipher[:] = body[i:i + 8]
        prev_intermediate[:] = dec
    return bytes(out)


async def build_padded(content):
    pad_len = (8 - (len(content) + 10) % 8) % 8
    return bytes([pad_len, 0, 0]) + b"\x00" * pad_len + content + b"\x00" * 7


async def encode_header(layout, send_option, cmd, order_id, flags, length, k, v80):
    out = bytearray()
    for code in layout:
        value = {0: send_option, 1: cmd, 2: order_id, 3: flags, 4: length}[code]
        if _FIELD_SIZES[code] == 1:
            out.append((value & 0xFF) ^ k)
        else:
            v = ((value & 0xFFFF) ^ v80) & 0xFFFF
            out.append(v & 0xFF)
            out.append((v >> 8) & 0xFF)
    return bytes(out)


async def crc7_buff(crc, buf):
    c = crc & 0x7F
    for b in buf:
        c = CRC7_TABLE[((2 * (c & 0xFF)) ^ (b & 0xFF)) & 0xFF] & 0x7F
    return c & 0x7F


async def sv_frame(msg_key, layout, send_option, cmd, order_id, flags, content, key, encrypted=True):
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(content), key) if encrypted else content
    hdr = bytearray([msg_key, 0]) + await encode_header(layout, send_option, cmd, order_id, flags, len(body), k, v80)
    packet = bytearray(hdr + body)
    packet[1] = await crc7_buff(0, bytes(packet[2:])) & 0x7F
    return bytes(packet)


async def build_match_startup_packets(token, udp_key, match_code, account_id, block_val,
                                      server_ip="", region="BD", client_version="1.132.6",
                                      client_version_code="2019121229", access_token=""):
    token = token.strip()
    udp_key = bytes.fromhex(udp_key)
    match_code = [int(ch) for ch in str(match_code).strip()]

    thunder_jwt = token[:660] if len(token) > 660 else token
    sharma_jwt = token[660:] if len(token) > 660 else ""
    encoded_thunder_jwt = thunder_jwt.encode() if isinstance(thunder_jwt, str) else thunder_jwt
    encoded_sharma_jwt = sharma_jwt.encode() if isinstance(sharma_jwt, str) else sharma_jwt

    garena420 = await has_ssan_zig(len(encoded_thunder_jwt)) + encoded_thunder_jwt

    reg = str(region).upper() if region else "BD"
    csoversea_block = bytes.fromhex(
        "ca0163736f7665727365612e7374726f6e67686f6c642e66726565666972656d6f62696c652e636f6d"
        "3b302e302e302e303b33342e3132362e37362e34353b33342e38372e3137372e31343b33342e38372e"
        "3137302e3233303b33352e3138352e3138332e35370000000000000100000000000000000000000001"
        "00000800000100000000000100a8a2d7bebd8d8bdf110200"
    )

    mid = bytes.fromhex('0000000001000102030101') + await has_ssan_zig(len(reg)) + reg.encode()
    mid += bytes.fromhex('0001030003000004')
    mid += await has_ssan_zig(len(client_version)) + client_version.encode()
    mid += await has_ssan_zig(len(client_version_code)) + client_version_code.encode()
    mid += csoversea_block

    clean_ip = server_ip.split(':')[0] if server_ip else "0.0.0.0"
    mid += await has_ssan_zig(len(clean_ip)) + clean_ip.encode()

    clean_acc_tok = access_token.strip() if access_token else ""
    if clean_acc_tok:
        mid += await has_ssan_zig(len(clean_acc_tok)) + clean_acc_tok.encode()

    mid += await has_ssan_zig(len(encoded_sharma_jwt)) + encoded_sharma_jwt

    tg_garena420 = (
        await uleb_encode(int(account_id)) +
        await uleb_encode(int(block_val)) +
        await uleb_encode(1) +
        await uleb_encode(43) +
        await uleb_encode(int(block_val)) +
        await uleb_encode(11) +
        mid
    )

    process = await sv_frame(0x5E, match_code, 2, 447, 0, 1, garena420, udp_key)
    loading = await sv_frame(0x5A, match_code, 2, 448, 1, 1, tg_garena420, udp_key)
    return process.hex(), loading.hex()


async def produce_xor_key(secret_key):
    k = secret_key[0] if secret_key and len(secret_key) > 0 else 10
    return k, ((k << 8) | k) & 0xFFFF


async def parse_layout(layout):
    if isinstance(layout, str):
        return [int(ch) for ch in layout.strip()]
    return list(layout)


async def build_hello_packet(text, key, layout):
    data = text.encode("utf-8")
    if len(data) > 25:
        raise ValueError(f"Text is too long ({len(data)} bytes)")
    content = b"\x10\x00\x00\x00" + data + b"\x00" * (29 - 4 - len(data))
    k, v80 = await produce_xor_key(key)
    layout = await parse_layout(layout)
    padded = await build_padded(content)
    enc_body = await tea_cbc_encrypt(padded, key)
    header_bytes = await encode_header(layout, 1, 1, 0, 1, len(enc_body), k, v80)
    packet = bytearray([0x63, 0x00]) + header_bytes + enc_body
    packet[1] = await crc7_buff(0, packet[2:]) & 0x7F
    return bytes(packet).hex()


async def classify(frame):
    cmd = frame["cmd"]
    msg_name = MESSAGE_ID_TO_NAME.get(cmd, f"UNKNOWN_{cmd}")
    if msg_name == "UDP_HELLO":
        return "HELLO"
    if msg_name == "UDP_ACK":
        return "ACK"
    if msg_name == "UDP_PING":
        return "PING"
    if msg_name == "RUDP_JOIN_MATCH":
        return "JOIN_MATCH"
    if msg_name.startswith("RUDP_"):
        return msg_name
    if msg_name.startswith("UDP_"):
        return msg_name
    return "DATA"


async def build_packet(msg_key, layout, send_option, cmd, order_id, flags, content, key, encrypted=True):
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(content), key) if encrypted else content
    hdr = bytearray([msg_key, 0])
    for code in layout:
        value = {0: send_option, 1: cmd, 2: order_id, 3: flags, 4: len(body)}[code]
        if _FIELD_SIZES[code] == 1:
            hdr.append((value & 0xFF) ^ k)
        else:
            v = ((value & 0xFFFF) ^ v80) & 0xFFFF
            hdr.append(v & 0xFF)
            hdr.append((v >> 8) & 0xFF)
    packet = bytearray(hdr + body)
    packet[1] = await crc7_buff(0, bytes(packet[2:])) & 0x7F
    return bytes(packet)


async def layouts_from_mask(mask):
    ru = [int(c) for c in str(mask).strip()]
    nr = [c for c in ru if c != 2]
    return ru, nr


async def reply_for(frame, key, mask, ack_key=0x68, ping_key=0x6D, hello_key=0x5B, ack_style="short"):
    ru, nr = await layouts_from_mask(mask)
    typ = await classify(frame)
    if typ == "HELLO":
        if ack_style == "echo":
            content = frame["content"] if frame["content"] else b"\x10\x00\x00\x00"
            return typ, await build_packet(hello_key, nr, 1, 1, None, 1, content, key)
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x01\x00", key)
    if typ == "ACK":
        content = frame["content"] if frame["content"] else b"\x01\x00"
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, content, key)
    if typ == "PING":
        c = frame["content"]
        counter = c[:4] if len(c) >= 4 else c
        return typ, await build_packet(ping_key, nr, 0, 3, None, 0, counter + b"\x00\x00\x00", key, encrypted=False)
    if typ == "JOIN_MATCH":
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x02\x00", key)
    return typ, None


async def keepalive_ping(sock, ip, port, key_bytes, mask, stop_event):
    nr = (await layouts_from_mask(mask))[1]
    ping_keys = [0x66, 0x6D, 0x69, 0x6C, 0x6B, 0x6E, 0x6F, 0x70]
    loop = asyncio.get_event_loop()
    i = 0
    while not stop_event.is_set():
        pk = ping_keys[i % len(ping_keys)]
        counter = int(time.time() * 1000) & 0xFFFFFFFF
        pkt = await build_packet(pk, nr, 0, 3, None, 0, struct.pack("<I", counter) + b"\x00\x00\x00", key_bytes, encrypted=False)
        try:
            await loop.sock_sendto(sock, pkt, (ip, port))
        except Exception:
            pass
        i += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass


async def try_header(buf, layout, k, v80):
    off = 2
    out = {}
    for code in layout:
        size = _FIELD_SIZES[code]
        if off + size > len(buf):
            return None
        out[_FIELD_NAMES[code]] = (buf[off] ^ k) if size == 1 else ((buf[off] | (buf[off + 1] << 8)) ^ v80) & 0xFFFF
        off += size
    out["headerLen"] = off
    return out


async def oicq_unpad(padded):
    if not padded or len(padded) < 8:
        return None
    if not all(padded[-1 - i] == 0 for i in range(7)):
        return None
    pad_len = padded[0] & 0x07
    s = 3 + pad_len
    e = len(padded) - 7
    return padded[s:e] if s < e else b""


async def decode_packet(packet, key, mask=None):
    data = bytes(packet) if isinstance(packet, bytes) else bytes.fromhex(packet)
    if len(data) < 8:
        return None
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    crc_ok = (data[1] & 0x7F) == await crc7_buff(0, data[2:])
    candidates = []
    if mask:
        ru, nr = await layouts_from_mask(mask)
        layouts = [("RUDP", ru), ("nonRUDP", nr)]
    else:
        layouts = [("RUDP", list(p)) for p in itertools.permutations([0, 1, 2, 3, 4])]
        layouts += [("nonRUDP", list(p)) for p in itertools.permutations([0, 1, 3, 4])]
    for kind, layout in layouts:
        f = await try_header(data, layout, k, v80)
        if not f:
            continue
        if f["flags"] > 7 or f["sendOption"] > 7:
            continue
        if f["length"] != len(data) - f["headerLen"]:
            continue
        body = data[f["headerLen"]:f["headerLen"] + f["length"]]
        content = None
        padded = None
        if f["flags"] & 1:
            if len(body) < 8 or len(body) % 8 != 0:
                continue
            padded = await tea_cbc_decrypt(body, key)
            content = await oicq_unpad(padded)
            if content is None:
                continue
        else:
            content = body
        score = (1 if crc_ok else 0) + (1 if content is not None else 0)
        candidates.append({
            "kind": kind, "layout": layout, "headerLen": f["headerLen"],
            "msgKey": data[0], "cmd": f["cmd"], "flags": f["flags"],
            "sendOption": f["sendOption"], "orderId": f.get("orderId"),
            "length": f["length"], "content": content, "crcOk": crc_ok,
            "padded": padded, "score": score, "total": len(data),
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c["kind"] == "RUDP" or c["kind"] == "nonRUDP", c["score"]), reverse=True)
    return candidates[0]


# ============================================================
# play_game
# ============================================================
async def play_game(server_ip_port, thunder, sharma, udp_key, match_code,
                    account_id, player_region, client_version, key, iv,
                    match_index: int):
    match_start_time = time.time()
    ping_task = None
    sock = None
    ping_stop = asyncio.Event()
    uid_str = str(account_id)
    completed_cleanly = False

    try:
        ip, port = server_ip_port.split(":")
        port = int(port)
        resolved_ip = await resolve_host_cloudflare(ip)

        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        optimize_udp_socket(sock)
        sock.setblocking(False)

        udp_key_bytes = bytes.fromhex(udp_key)
        hello_packet = await build_hello_packet(f"{account_id}_2585", udp_key_bytes, match_code)
        await loop.sock_sendto(sock, bytes.fromhex(hello_packet), (resolved_ip, port))

        ack_state = "waiting_for_hello_reply"
        thunder_sent = False
        sharma_sent = False
        join_match_received = False
        local_closed = False
        send_lock = asyncio.Lock()

        ping_task = asyncio.create_task(
            keepalive_ping(sock, resolved_ip, port, udp_key_bytes, match_code, ping_stop)
        )
        last_activity = time.time()
        MAX_IDLE_BEFORE_HELLO_RESEND = 7.0

        print_colored(
            f"🎮 [MATCH #{match_index}] UDP started → {server_ip_port} (DNS: {resolved_ip})",
            Colors.MAGENTA
        )

        async def send_thunder_sharma_inline():
            nonlocal ack_state, thunder_sent, sharma_sent
            if thunder_sent:
                return
            async with send_lock:
                if thunder_sent:
                    return
                try:
                    await loop.sock_sendto(sock, bytes.fromhex(thunder), (resolved_ip, port))
                    thunder_sent = True
                    await asyncio.sleep(0.1)
                    prepare_ack = await build_packet(
                        0x68, (await layouts_from_mask(match_code))[1],
                        0, 2, None, 1, b"\x01\x00", udp_key_bytes
                    )
                    await loop.sock_sendto(sock, prepare_ack, (resolved_ip, port))
                    await asyncio.sleep(0.2)
                    await loop.sock_sendto(sock, bytes.fromhex(sharma), (resolved_ip, port))
                    sharma_sent = True
                    ack_state = "thunder_sharma_sent"
                    print_success(f"[MATCH #{match_index}] Thunder+Sharma sent!")
                except Exception as e:
                    print_error(f"[MATCH #{match_index}] send error: {e}")

        while not local_closed:
            if time.time() - match_start_time > MAX_MATCH_DURATION:
                break
            try:
                response, server_addr = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 65535), timeout=1.5
                )
                if response:
                    last_activity = time.time()
                    frame = await decode_packet(response, udp_key_bytes, match_code)
                    if frame:
                        ptype = await classify(frame)

                        if frame['cmd'] in [103, 107]:
                            print_success(
                                f"[MATCH #{match_index}] Completed (cmd {frame['cmd']})"
                            )
                            completed_cleanly = True
                            local_closed = True
                            continue

                        if frame['cmd'] == 101:
                            try:
                                ack_pkt = await build_packet(
                                    0x68, (await layouts_from_mask(match_code))[1],
                                    0, 2, None, 1, b"\x01\x00", udp_key_bytes
                                )
                                await loop.sock_sendto(sock, ack_pkt, server_addr)
                            except Exception:
                                pass
                            continue

                        if ptype in ["ACK", "PING", "HELLO", "JOIN_MATCH"]:
                            if ptype == "HELLO" and ack_state == "waiting_for_hello_reply":
                                typ, reply = await reply_for(
                                    frame, udp_key_bytes, match_code, ack_style="short"
                                )
                                if reply:
                                    await loop.sock_sendto(sock, reply, server_addr)
                                ack_state = "ack_sent_waiting"
                            elif ptype == "ACK":
                                if ack_state == "waiting_for_hello_reply":
                                    typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                    if reply:
                                        await loop.sock_sendto(sock, reply, server_addr)
                                    ack_state = "ready_to_send_thunder"
                                elif ack_state == "ack_sent_waiting":
                                    ack_state = "ready_to_send_thunder"
                                else:
                                    typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                    if reply:
                                        await loop.sock_sendto(sock, reply, server_addr)
                            elif ptype == "PING":
                                typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                if reply:
                                    await loop.sock_sendto(sock, reply, server_addr)
                            elif ptype == "JOIN_MATCH" and not join_match_received:
                                typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                if reply:
                                    await loop.sock_sendto(sock, reply, server_addr)
                                    join_match_received = True
            except asyncio.TimeoutError:
                if ack_state == "ready_to_send_thunder" and not thunder_sent:
                    await send_thunder_sharma_inline()
                elif ack_state == "waiting_for_hello_reply":
                    if (time.time() - last_activity) > MAX_IDLE_BEFORE_HELLO_RESEND:
                        try:
                            pkt = await build_hello_packet(
                                f"{account_id}_2585", udp_key_bytes, match_code
                            )
                            await loop.sock_sendto(sock, bytes.fromhex(pkt), (resolved_ip, port))
                        except Exception:
                            pass
                        last_activity = time.time()
                    if (time.time() - match_start_time) > 25.0:
                        print_warning(f"[MATCH #{match_index}] Handshake timeout")
                        break
                elif ack_state == "thunder_sharma_sent":
                    if (time.time() - last_activity) > MATCH_IDLE_TIMEOUT:
                        print_success(f"[MATCH #{match_index}] Finished naturally")
                        completed_cleanly = True
                        break
                continue
            except BlockingIOError:
                await asyncio.sleep(0.05)
            except OSError:
                await asyncio.sleep(0.5)
                continue
            except Exception:
                await asyncio.sleep(0.5)
                continue

            if ack_state == "ready_to_send_thunder" and not thunder_sent:
                await send_thunder_sharma_inline()

        return f"match #{match_index} finished"
    except Exception as e:
        print_error(f"[MATCH #{match_index}] error: {e}")
        return f"match #{match_index} error"
    finally:
        if completed_cleanly:
            try:
                bot_state.increment_match(uid_str)
            except Exception:
                pass
        ping_stop.set()
        if ping_task:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        remaining = await _dec_match(uid_str)
        total = await _get_total_match_count()
        print_info(
            f"[MATCH #{match_index}] Closed. "
            f"UID active: {remaining} | Total active: {total}"
        )
        try:
            bot_state.update_status(uid_str, "IN_MATCH" if remaining > 0 else "ONLINE", remaining)
        except Exception:
            pass


# ============================================================
# functional_lone_wolf
# ============================================================
async def functional_lone_wolf(addrs, starter_packet, account_region, client_version,
                                key, iv, account_id="", account_data=None,
                                max_reconnects=10):
    reconnects = 0
    ip, port = addrs.split(":")
    play_matches: List[asyncio.Task] = []
    no_response_count = 0
    search_attempts = 0
    last_start_time = 0.0
    uid_str = str(account_id)

    consecutive_parse_failures = 0

    current_token = starter_packet
    current_key = key
    current_iv = iv
    current_account_data = account_data

    try:
        while True:
            writer = None
            try:
                if current_account_data:
                    fresh = None
                    if current_account_data.get('auth_type') == 'guest' and current_account_data.get('auth_uid'):
                        fresh = cache_get(str(current_account_data['auth_uid']))
                    elif current_account_data.get('auth_type') == 'token' and current_account_data.get('auth_token'):
                        fresh = cache_get(f"tok_{current_account_data['auth_token'][:20]}")

                    if fresh:
                        current_account_data = fresh
                        current_key = fresh['aes_ak']
                        current_iv = fresh['iv_i']
                        current_token = await build_tcp_startup_packet(
                            fresh['account_id'],
                            fresh['token'],
                            fresh['server_time'],
                            current_key,
                            current_iv,
                            region=fresh.get('region', account_region),
                            typ='OnLine'
                        )
                    else:
                        print_warning(f"[FUNCTIONAL] Cache miss for {uid_str} → re-login needed")
                        try:
                            if current_account_data.get('auth_uid'):
                                cache_invalidate(str(current_account_data['auth_uid']))
                            if current_account_data.get('auth_token'):
                                cache_invalidate(f"tok_{current_account_data['auth_token'][:20]}")
                        except Exception:
                            pass
                        raise ConnectionError("Cache expired, triggering fresh login")

                resolved_ip = await resolve_host_cloudflare(ip)
                reader, writer = await asyncio.open_connection(resolved_ip, int(port))

                raw_sock = writer.get_extra_info('socket')
                if raw_sock:
                    optimize_tcp_socket(raw_sock)

                writer.write(bytes.fromhex(current_token))
                await writer.drain()

                try:
                    init_ka = await send_keep_alive(account_region)
                    if init_ka and writer and not writer.is_closing():
                        writer.write(init_ka)
                        await asyncio.wait_for(writer.drain(), timeout=3)
                except Exception:
                    pass

                print_success(f"[FUNCTIONAL] TCP Gateway Connected for UID: {uid_str} (DNS: {resolved_ip})")
                reconnects = 0
                no_response_count = 0
                last_start_time = 0.0

                async def send_start_match():
                    nonlocal search_attempts, last_start_time
                    search_attempts += 1
                    current_region = account_region or "BD"
                    print_info(f"[LONE WOLF] Sending StartMatch #{search_attempts} region: {current_region}")
                    try:
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                        await start_game_lone_wolf(
                            current_region, client_version, writer,
                            current_key, current_iv
                        )
                        print_success(f"[LONE WOLF] StartMatch packet sent")
                        active = await _get_match_count(uid_str)
                        try:
                            bot_state.update_status(uid_str, "SEARCHING", active)
                        except Exception:
                            pass
                    except Exception as e:
                        print_error(f"[LONE WOLF] start_game error: {e}")
                    last_start_time = asyncio.get_running_loop().time()

                await send_start_match()

                while True:
                    play_matches[:] = [m for m in play_matches if not m.done()]

                    active_count = await _get_match_count(uid_str)
                    try:
                        bot_state.update_status(
                            uid_str,
                            "ONLINE" if active_count == 0 else "IN_MATCH",
                            active_count
                        )
                    except Exception:
                        pass

                    now = asyncio.get_running_loop().time()
                    if now - last_start_time >= START_MATCH_INTERVAL:
                        await send_start_match()

                    try:
                        data = await asyncio.wait_for(reader.read(8192), timeout=0.5)
                    except asyncio.TimeoutError:
                        no_response_count += 1
                        if no_response_count > 80:
                            print_warning(f"[FUNCTIONAL] Gateway silent ({uid_str}). Reconnecting...")
                            raise ConnectionError("Gateway idle timeout")
                        continue

                    if not data:
                        raise ConnectionError("Connection closed by server")

                    hex_data = data.hex()
                    packet_length = len(data)
                    no_response_count = 0

                    if hex_data.startswith("0300") and 10 < packet_length < 30:
                        print_info(f"[TCP←RECV] Match Queue Confirmed")
                        continue

                    if hex_data.startswith("0300") and packet_length >= 300:
                        print_colored("=" * 60, Colors.GREEN)
                        print_colored(f"MATCH FOUND! Loading...", Colors.GREEN)
                        print_colored("=" * 60, Colors.GREEN)

                        try:
                            res = json.loads(await decode_protobuf(hex_data[10:]))
                            token = None
                            udp_key = None
                            match_code = None
                            server_ip_port = None
                            match_account_id = None
                            block_val = None

                            if '42' in res and 'data' in res['42']:
                                match_code = res['42']['data']
                            if '5' in res and 'data' in res['5']:
                                res_field5 = res['5']['data']
                                server_ip_port = res_field5.get('2', {}).get('data')
                                udp_key = res_field5.get('3', {}).get('data')
                                token = res_field5.get('4', {}).get('data')
                                if '42' in res_field5:
                                    match_code = res_field5['42']['data']
                            if '1' in res and 'data' in res['1']:
                                match_account_id = res['1']['data']
                            if '5' in res and 'data' in res['5']:
                                block_val = res['5']['data'].get('1', {}).get('data')

                            effective_acc_id = match_account_id or account_id or "BD_BOT"

                            if token and udp_key and match_code and server_ip_port:
                                acc_tok = ""
                                if current_account_data:
                                    acc_tok = current_account_data.get('access_token', '') or ""
                                thunder, sharma = await build_match_startup_packets(
                                    token, udp_key, match_code, effective_acc_id, block_val or 0,
                                    server_ip=server_ip_port,
                                    region=account_region,
                                    client_version=client_version,
                                    access_token=acc_tok
                                )

                                match_index = await _inc_match(uid_str)
                                total = await _get_total_match_count()
                                print_colored(
                                    f"🚀 [MATCH #{match_index}] UDP starting → {server_ip_port}",
                                    Colors.CYAN
                                )

                                new_match = asyncio.create_task(
                                    play_game(
                                        server_ip_port,
                                        thunder,
                                        sharma,
                                        udp_key,
                                        match_code,
                                        effective_acc_id,
                                        "BD",
                                        client_version,
                                        current_key,
                                        current_iv,
                                        match_index=match_index
                                    )
                                )
                                play_matches.append(new_match)
                                consecutive_parse_failures = 0

                                try:
                                    writer.close()
                                    await writer.wait_closed()
                                except Exception:
                                    pass

                                await asyncio.sleep(NEW_MATCH_DELAY)
                                reconnects = 0
                                break
                            else:
                                consecutive_parse_failures += 1
                                print_warning(f"[FUNCTIONAL] Non-match big packet (#{consecutive_parse_failures})")
                                if consecutive_parse_failures >= MAX_CONSECUTIVE_PARSE_FAILURES:
                                    if current_account_data:
                                        try:
                                            if current_account_data.get('auth_uid'):
                                                cache_invalidate(str(current_account_data['auth_uid']))
                                        except Exception:
                                            pass
                                    consecutive_parse_failures = 0
                                try:
                                    writer.close()
                                    await writer.wait_closed()
                                except Exception:
                                    pass
                                await asyncio.sleep(NON_MATCH_RECONNECT_DELAY)
                                break
                        except Exception as e:
                            print_error(f"[FUNCTIONAL] Match packet error: {e}")
                            try:
                                writer.close()
                                await writer.wait_closed()
                            except Exception:
                                pass
                            await asyncio.sleep(NON_MATCH_RECONNECT_DELAY)
                            break

                    if 30 <= packet_length <= 40:
                        continue

            except asyncio.CancelledError:
                for m in play_matches:
                    if not m.done():
                        m.cancel()
                if play_matches:
                    await asyncio.gather(*play_matches, return_exceptions=True)
                play_matches.clear()
                raise
            except Exception as e:
                print_error(f"[FUNCTIONAL] TCP state ({uid_str}): {e}")
                play_matches[:] = [m for m in play_matches if not m.done()]
                if writer:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                if "Cache expired" in str(e):
                    print_warning(f"[FUNCTIONAL] Triggering re-login for {uid_str}")
                    break
                reconnects += 1
                if reconnects > max_reconnects:
                    reconnects = 0
                    await asyncio.sleep(3)
                    continue
                await asyncio.sleep(min(reconnects, 2))
    except asyncio.CancelledError:
        for m in play_matches:
            if not m.done():
                m.cancel()
        if play_matches:
            await asyncio.gather(*play_matches, return_exceptions=True)
        play_matches.clear()
        raise


async def informational(addrs, starter_packet, key, iv, region="BD", max_reconnects=3):
    reconnects = 0
    ip, port = addrs.split(":")
    while True:
        writer = None
        ping_task = None
        try:
            resolved_ip = await resolve_host_cloudflare(ip)
            reader, writer = await asyncio.open_connection(resolved_ip, int(port))

            raw_sock = writer.get_extra_info('socket')
            if raw_sock:
                optimize_tcp_socket(raw_sock)

            writer.write(bytes.fromhex(starter_packet))
            await writer.drain()
            reconnects = 0

            try:
                init_ka = await send_keep_alive(region)
                if init_ka and writer and not writer.is_closing():
                    writer.write(init_ka)
                    await asyncio.wait_for(writer.drain(), timeout=3)
            except Exception:
                pass

            async def info_keepalive():
                ka_bytes = await send_keep_alive(region)
                while True:
                    await asyncio.sleep(5)
                    try:
                        if writer and not writer.is_closing():
                            writer.write(ka_bytes)
                            await writer.drain()
                    except Exception:
                        break

            ping_task = asyncio.create_task(info_keepalive())

            while True:
                data = await reader.read(8192)
                if not data:
                    raise ConnectionError("Connection closed")
        except asyncio.CancelledError:
            if ping_task:
                ping_task.cancel()
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            raise
        except Exception:
            if ping_task:
                ping_task.cancel()
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            reconnects += 1
            if reconnects > max_reconnects:
                await asyncio.sleep(3)
                reconnects = 0
            else:
                await asyncio.sleep(1)


# ==================== ACCOUNT PROCESSORS ====================
def _register_credentials(account_data: Dict):
    try:
        acc_id = str(account_data['account_id'])
        bot_state.account_credentials[acc_id] = account_data
        if account_data.get('auth_uid'):
            bot_state.account_credentials[str(account_data['auth_uid'])] = account_data
        if account_data.get('auth_token'):
            bot_state.account_credentials[f"tok_{account_data['auth_token'][:20]}"] = account_data
    except Exception:
        pass


async def refresh_account_profile(account_data_or_uid: Any):
    """Refresh level/exp/likes/nickname/region from GetPlayerPersonalShow."""
    try:
        if isinstance(account_data_or_uid, str):
            uid = str(account_data_or_uid)
            account_data = bot_state.account_credentials.get(uid)
        else:
            account_data = account_data_or_uid
            uid = str(account_data.get('account_id'))

        if not account_data:
            return

        acc_id = str(account_data['account_id'])
        region = account_data.get('region', 'BD')
        bearer = account_data.get('token')

        if not bearer:
            print_warning(f"[EXP-REFRESH] No bearer token for UID {acc_id}")
            return

        profile = await fetch_player_profile(acc_id, region, bearer)
        if not profile:
            print_warning(f"[EXP-REFRESH] Profile fetch failed for {acc_id}")
            return

        level = profile['level']
        exp = profile['exp']
        likes = profile['likes']
        nickname = profile['nickname']

        if exp > 0 or level > 0:
            bot_state.update_exp(acc_id, exp, level)
        if likes > 0 and acc_id in bot_state.accounts:
            bot_state.accounts[acc_id]["likes"] = likes
        if nickname and acc_id in bot_state.accounts:
            bot_state.accounts[acc_id]["nickname"] = nickname
        if profile['region'] and acc_id in bot_state.accounts:
            bot_state.accounts[acc_id]["region"] = profile['region']
        print_info(f"[EXP-REFRESH] UID {acc_id} -> Level: {level}, EXP: {exp}")
    except Exception as e:
        print_error(f"refresh_account_profile error: {e}")


async def process_account_uid_pass(uid: str, password: str) -> Optional[Dict]:
    cached = cache_get(uid)
    if cached:
        print_success(f"[CACHE HIT] UID {uid} loaded from token_cache.json (no login)")
        acc_id = str(cached['account_id'])
        bot_state.register_account(
            uid=acc_id,
            nickname=cached.get('nickname', f"Player_{acc_id}"),
            region=cached.get('region', 'BD'),
            level=cached.get('level', 1),
            exp=cached.get('exp', 0),
            likes=cached.get('likes', 0)
        )
        _register_credentials(cached)
        try:
            bot_state.set_alias(str(uid), acc_id)
        except Exception:
            pass
        # ✅ Profile refresh — fail ho to account registered rahe
        try:
            profile = await fetch_player_profile(
                acc_id,
                cached.get('region', 'BD'),
                cached.get('token')
            )
            if profile:
                bot_state.register_account(
                    uid=acc_id,
                    nickname=profile['nickname'] or cached.get('nickname', f"Player_{acc_id}"),
                    region=profile['region'] or cached.get('region', 'BD'),
                    level=profile['level'],
                    exp=profile['exp'],
                    likes=profile['likes']
                )
                cached['nickname'] = profile['nickname'] or cached.get('nickname', f"Player_{acc_id}")
                cached['region'] = profile['region'] or cached.get('region', 'BD')
                cached['level'] = profile['level']
                cached['exp'] = profile['exp']
                cached['likes'] = profile['likes']
                cache_set(uid, cached)
        except Exception as e:
            print_error(f"[CACHE HIT] Profile refresh failed: {e}")
        return cached

    print_info(f"[LOGIN] Full login for UID {uid}...")
    try:
        print_info(f"[STEP 1/4] Fetching version config...")
        verconfig_res = await version_config()
        if verconfig_res is None:
            print_error(f"[STEP 1/4] FAILED")
            return None
        release_version, remote_version, server_url = verconfig_res
        client_version = remote_version or "1.132.8"
        print_success(f"[STEP 1/4] OK → release={release_version} | client={client_version} | server={server_url}")

        print_info(f"[STEP 2/4] Guest OAuth token grant for UID {uid}...")
        tokengrant_response = await get_access_token(uid, password)
        if tokengrant_response is None:
            print_error(f"[STEP 2/4] FAILED")
            return None
        open_id, access_token, platform = tokengrant_response
        print_success(f"[STEP 2/4] OK → open_id={open_id[:12]}... | platform={platform}")

        device_info = get_device_for_account(uid)
        print_info(f"[DEVICE] Using: {device_info.get('model')} | {device_info.get('gpu_renderer')}")

        print_info(f"[STEP 3/4] Building MajorLogin payload + POST...")
        login_payload_data = await build_majorlogin_payload(open_id, access_token, platform, client_version, device_info)
        if login_payload_data is None:
            print_error(f"[STEP 3/4] FAILED")
            return None
        majorlogin_response = await send_majorlogin(login_payload_data, release_version, server_url)
        if majorlogin_response is None:
            print_error(f"[STEP 3/4] FAILED")
            return None
        print_success(f"[STEP 3/4] OK → account_id={majorlogin_response.account_id} | region={majorlogin_response.region}")

        acc_id = str(majorlogin_response.account_id)

        # ✅ Alias turant set karo
        try:
            bot_state.set_alias(str(uid), acc_id)
        except Exception:
            pass

        # ✅ Default values — inhe pehle register karo taaki account dashboard me dikh jaye
        nickname = f"Player_{acc_id[-6:]}"
        region = majorlogin_response.region or "BD"
        level = 1
        exp = 0
        likes = 0

        bot_state.register_account(
            uid=acc_id, nickname=nickname, region=region,
            level=level, exp=exp, likes=likes
        )

        # -------- Step 4: GetLoginData (fail ho to bhi account registered rahe) --------
        print_info(f"[STEP 4/4] Fetching login data (GetLoginData)...")
        getlogin_result = await send_getlogin(
            login_payload_data,
            majorlogin_response.url,
            majorlogin_response.token,
            release_version
        )

        functional_addrs = ""
        informational_addrs = ""

        if getlogin_result is None:
            print_warning(f"[STEP 4/4] GetLoginData failed — registering with defaults")
        else:
            res_proto, dict_res = getlogin_result
            functional_addrs = res_proto.functional_addrs or get_proto_field(dict_res, 14) or ""
            informational_addrs = res_proto.informational_addrs or get_proto_field(dict_res, 32) or ""
            if res_proto.nickname:
                nickname = res_proto.nickname
            print_success(f"[STEP 4/4] OK → nickname={res_proto.nickname} | func={bool(functional_addrs)} | info={bool(informational_addrs)}")

        # -------- Profile fetch (best effort) --------
        try:
            profile = await fetch_player_profile(acc_id, region, majorlogin_response.token)
            if profile:
                nickname = profile['nickname'] or nickname
                region = profile['region'] or region
                level = profile['level']
                exp = profile['exp']
                likes = profile['likes']
        except Exception as e:
            print_error(f"[PROFILE] Initial fetch error: {e}")

        # ✅ Final register with real data
        bot_state.register_account(
            uid=acc_id, nickname=nickname, region=region,
            level=level, exp=exp, likes=likes
        )

        account_data = {
            'account_id': majorlogin_response.account_id,
            'nickname': nickname,
            'region': region,
            'level': level,
            'exp': exp,
            'likes': likes,
            'open_id': open_id,
            'access_token': access_token,
            'platform': str(platform),
            'token': majorlogin_response.token,
            'server_time': majorlogin_response.server_time,
            'aes_ak': majorlogin_response.aes_ak,
            'iv_i': majorlogin_response.iv_i,
            'functional_addrs': functional_addrs,
            'informational_addrs': informational_addrs,
            'release_version': release_version,
            'client_version': client_version,
            'server_url': majorlogin_response.url,
            'login_payload_data': login_payload_data,
            'auth_type': 'guest',
            'auth_uid': uid,
            'auth_password': password
        }
        _register_credentials(account_data)
        cache_set(uid, account_data)
        return account_data
    except Exception as e:
        import traceback
        print_error(f"process_account_uid_pass error: {e}")
        traceback.print_exc()
        return None


async def process_account_token(access_token: str) -> Optional[Dict]:
    cache_key = f"tok_{access_token[:20]}"
    cached = cache_get(cache_key)
    if cached:
        print_success(f"[CACHE HIT] Token {access_token[:10]}... loaded from cache")
        acc_id = str(cached['account_id'])
        bot_state.register_account(
            uid=acc_id,
            nickname=cached.get('nickname', f"Player_{acc_id}"),
            region=cached.get('region', 'BD'),
            level=cached.get('level', 1),
            exp=cached.get('exp', 0),
            likes=cached.get('likes', 0)
        )
        _register_credentials(cached)
        try:
            bot_state.set_alias(cache_key, acc_id)
        except Exception:
            pass
        try:
            profile = await fetch_player_profile(
                acc_id,
                cached.get('region', 'BD'),
                cached.get('token')
            )
            if profile:
                bot_state.register_account(
                    uid=acc_id,
                    nickname=profile['nickname'] or cached.get('nickname', f"Player_{acc_id}"),
                    region=profile['region'] or cached.get('region', 'BD'),
                    level=profile['level'],
                    exp=profile['exp'],
                    likes=profile['likes']
                )
                cached['nickname'] = profile['nickname'] or cached.get('nickname', f"Player_{acc_id}")
                cached['region'] = profile['region'] or cached.get('region', 'BD')
                cached['level'] = profile['level']
                cached['exp'] = profile['exp']
                cached['likes'] = profile['likes']
                cache_set(cache_key, cached)
        except Exception as e:
            print_error(f"[CACHE HIT] Token profile refresh failed: {e}")
        return cached

    print_info("[LOGIN] Full login with Access Token...")
    try:
        verconfig_res = await version_config()
        if verconfig_res is None:
            return None
        release_version, client_version, server_url = verconfig_res

        import requests
        url = f"https://100067.connect.garena.com/oauth/token/inspect?token={access_token}"
        hdrs = {
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "close",
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": "100067.connect.garena.com",
            "User-Agent": "GarenaMSDK/4.0.19P4(G011A ;Android 9;en;US;)"
        }
        resp = await asyncio.to_thread(requests.get, url, headers=hdrs, timeout=10)
        data = resp.json()

        if 'error' in data:
            return None

        open_id = data.get('open_id')
        platform = data.get('platform', 4)

        if not open_id:
            return None

        device_info = get_device_for_account(open_id)

        login_payload_data = await build_majorlogin_payload(open_id, access_token, str(platform), client_version, device_info)
        if not login_payload_data:
            return None

        majorlogin_response = await send_majorlogin(login_payload_data, release_version, server_url)
        if majorlogin_response is None:
            return None

        acc_id = str(majorlogin_response.account_id)

        try:
            bot_state.set_alias(cache_key, acc_id)
        except Exception:
            pass

        nickname = f"Player_{acc_id[-6:]}"
        region = majorlogin_response.region or "BD"
        level = 1
        exp = 0
        likes = 0

        bot_state.register_account(
            uid=acc_id, nickname=nickname, region=region,
            level=level, exp=exp, likes=likes
        )

        getlogin_result = await send_getlogin(
            login_payload_data,
            majorlogin_response.url,
            majorlogin_response.token,
            release_version
        )

        functional_addrs = ""
        informational_addrs = ""

        if getlogin_result is None:
            print_warning(f"[TOKEN] GetLoginData failed — registering with defaults")
        else:
            res_proto, dict_res = getlogin_result
            functional_addrs = res_proto.functional_addrs or get_proto_field(dict_res, 14) or ""
            informational_addrs = res_proto.informational_addrs or get_proto_field(dict_res, 32) or ""
            if res_proto.nickname:
                nickname = res_proto.nickname

        try:
            profile = await fetch_player_profile(acc_id, region, majorlogin_response.token)
            if profile:
                nickname = profile['nickname'] or nickname
                region = profile['region'] or region
                level = profile['level']
                exp = profile['exp']
                likes = profile['likes']
        except Exception as e:
            print_error(f"[PROFILE] Token initial fetch error: {e}")

        bot_state.register_account(
            uid=acc_id, nickname=nickname, region=region,
            level=level, exp=exp, likes=likes
        )

        account_data = {
            'account_id': majorlogin_response.account_id,
            'nickname': nickname,
            'region': region,
            'level': level,
            'exp': exp,
            'likes': likes,
            'open_id': open_id,
            'access_token': access_token,
            'platform': str(platform),
            'token': majorlogin_response.token,
            'server_time': majorlogin_response.server_time,
            'aes_ak': majorlogin_response.aes_ak,
            'iv_i': majorlogin_response.iv_i,
            'functional_addrs': functional_addrs,
            'informational_addrs': informational_addrs,
            'release_version': release_version,
            'client_version': client_version,
            'server_url': majorlogin_response.url,
            'login_payload_data': login_payload_data,
            'auth_type': 'token',
            'auth_token': access_token
        }
        _register_credentials(account_data)
        cache_set(cache_key, account_data)
        return account_data
    except Exception as e:
        import traceback
        print_error(f"process_account_token error: {e}")
        traceback.print_exc()
        return None


async def run_account_worker(account_data: Dict, label: str):
    acc_id = str(account_data['account_id'])
    informational_task = None
    exp_task = None
    try:
        reg = account_data.get('region', 'BD')
        tcp_packet_online = await build_tcp_startup_packet(
            account_data['account_id'],
            account_data['token'],
            account_data['server_time'],
            account_data['aes_ak'],
            account_data['iv_i'],
            region=reg,
            typ='OnLine'
        )

        tcp_packet_chat = await build_tcp_startup_packet(
            account_data['account_id'],
            account_data['token'],
            account_data['server_time'],
            account_data['aes_ak'],
            account_data['iv_i'],
            region=reg,
            typ='ChaT'
        )

        if account_data.get('informational_addrs'):
            informational_task = asyncio.create_task(
                informational(
                    account_data['informational_addrs'],
                    tcp_packet_chat,
                    account_data['aes_ak'],
                    account_data['iv_i'],
                    region=reg
                )
            )

        async def exp_refresher():
            while True:
                await asyncio.sleep(PROFILE_REFRESH_INTERVAL)
                try:
                    fresh = bot_state.account_credentials.get(acc_id)
                    if not fresh:
                        continue
                    profile = await fetch_player_profile(
                        fresh.get('account_id', acc_id),
                        fresh.get('region', 'BD'),
                        fresh.get('token')
                    )
                    if not profile:
                        continue
                    new_exp = profile['exp']
                    new_level = profile['level']
                    cur = bot_state.accounts.get(str(acc_id))
                    if cur and (new_exp > 0 or new_level > 0):
                        bot_state.update_exp(str(acc_id), new_exp, new_level)
                    if cur and profile['likes'] > 0:
                        cur['likes'] = profile['likes']
                    if cur and profile['nickname']:
                        cur['nickname'] = profile['nickname']
                    if cur and profile['region']:
                        cur['region'] = profile['region']
                except Exception as e:
                    print_error(f"[EXP-REFRESHER] {acc_id}: {e}")

        exp_task = asyncio.create_task(exp_refresher())

        if account_data.get('functional_addrs'):
            functional_task = asyncio.create_task(
                functional_lone_wolf(
                    account_data['functional_addrs'],
                    tcp_packet_online,
                    account_data['region'],
                    account_data['client_version'],
                    account_data['aes_ak'],
                    account_data['iv_i'],
                    account_id=acc_id,
                    account_data=account_data
                )
            )
            await functional_task
        else:
            print_error(f"[WORKER] No functional_addrs for {acc_id} — cannot start match loop")
            while True:
                await asyncio.sleep(60)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        print_error(f"run_account_worker error for {label}: {e}")
    finally:
        for t in (informational_task, exp_task):
            if t and not t.done():
                t.cancel()
        for t in (informational_task, exp_task):
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


async def account_loop_guest(uid: str, password: str):
    while True:
        try:
            print_info(f"[LOGIN] Starting login for Guest UID: {uid}...")
            try:
                bot_state.update_status(str(uid), "CONNECTING")
            except Exception:
                pass
            account_data = await process_account_uid_pass(uid, password)
            if not account_data:
                print_error(f"Login failed for UID: {uid}. Retrying in 15 seconds...")
                try:
                    bot_state.update_status(str(uid), "ERROR")
                except Exception:
                    pass
                await asyncio.sleep(15)
                continue

            acc_id = str(account_data.get('account_id', uid))

            if acc_id in bot_state.accounts:
                current_exp = int(bot_state.accounts[acc_id].get('current_exp') or account_data.get('exp', 0) or 0)
                current_level = int(bot_state.accounts[acc_id].get('level') or account_data.get('level', 1) or 1)
            else:
                current_exp = int(account_data.get('exp', 0) or 0)
                current_level = int(account_data.get('level', 1) or 1)

            # CS engine routing for early levels
            if 48 <= current_exp < 202:
                print_warning(f"[ROUTING] UID {uid} | Level {current_level} | exp={current_exp} → CS Engine")
                try:
                    bot_state.update_status(acc_id, "CS_ENGINE", 1)
                except Exception:
                    pass
                try:
                    if os.path.exists("cs.py"):
                        sub_env = os.environ.copy()
                        sub_env["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
                        proc = await asyncio.create_subprocess_exec(
                            sys.executable, "cs.py", str(uid), str(password),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.STDOUT,
                            env=sub_env
                        )
                        while True:
                            line = await proc.stdout.readline()
                            if not line:
                                break
                            log_msg = line.decode('utf-8', errors='replace').strip()
                            if log_msg:
                                print_colored(f"[CS-ENGINE:{uid}] {log_msg}", Colors.CYAN)
                                bot_state.log(log_msg, "info", acc_id)
                        await proc.wait()
                    else:
                        print_warning("[CS-ENGINE] cs.py not found, skipping")
                except Exception as e:
                    print_error(f"[CS-ENGINE] Subprocess error: {e}")

                await asyncio.sleep(3)
                try:
                    await refresh_account_profile(account_data)
                except Exception:
                    pass

                if acc_id in bot_state.accounts:
                    current_exp = int(bot_state.accounts[acc_id].get('current_exp') or current_exp)
                    current_level = int(bot_state.accounts[acc_id].get('level') or current_level)

                if current_exp >= 202:
                    print_success(f"[ROUTING] 🎉 UID {uid} leveled up! → Lone Wolf mode.")
                    account_data['exp'] = current_exp
                    account_data['level'] = current_level
                    cache_set(str(uid), account_data)
                else:
                    print_warning(f"[ROUTING] UID {uid} still exp={current_exp} (<202). Retrying CS in 5s...")
                    cache_invalidate(str(uid))
                    await asyncio.sleep(5)
                    continue

            await run_account_worker(account_data, uid)
            print_warning(f"Session finished for {uid}. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            print_warning(f"Worker for {uid} stopped.")
            try:
                bot_state.update_status(str(uid), "OFFLINE")
            except Exception:
                pass
            break
        except Exception as e:
            print_error(f"Error for UID {uid}: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


async def account_loop_token(token: str):
    token_label = token[:10]
    while True:
        try:
            print_info("[LOGIN] Starting login with Access Token...")
            account_data = await process_account_token(token)
            if not account_data:
                print_error("Login failed for Token. Retrying in 15 seconds...")
                await asyncio.sleep(15)
                continue

            acc_id = str(account_data['account_id'])
            await run_account_worker(account_data, acc_id)
            print_warning("Token session finished. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            print_warning(f"Worker for token {token_label} stopped.")
            break
        except Exception as e:
            print_error(f"Token error: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


# ==================== ACCOUNTS LOADER ====================
def load_accounts():
    accounts = []
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    accounts = data
        except Exception as e:
            print_error(f"Could not load {ACCOUNTS_FILE}: {e}")

    if not accounts and FALLBACK_UID and FALLBACK_PASSWORD:
        accounts.append({"uid": FALLBACK_UID, "password": FALLBACK_PASSWORD})

    return accounts


# ==================== MAIN (Render-ready) ====================
async def main():
    print_colored("=" * 60, Colors.CYAN)
    print_colored("    TEAM 84FF - FreeFire Level Up Bot (RENDER BUILD)", Colors.GREEN)
    print_colored(f"   Host: {WEB_HOST} | Port: {WEB_PORT} | Data: {DATA_DIR}", Colors.WHITE)
    print_colored("=" * 60, Colors.CYAN)

    try:
        ip = await get_public_ip()
        print_success(f"[STARTUP] Egress IP: {ip}")
    except Exception:
        pass

    # -------- Start Web Dashboard FIRST --------
    try:
        await start_web_dashboard(host=WEB_HOST, port=WEB_PORT)
        if PUBLIC_BASE_URL:
            print_success(f"Public URL: {PUBLIC_BASE_URL}")
        print_success(f"Web Dashboard live at http://{WEB_HOST}:{WEB_PORT}")
    except Exception as e:
        print_error(f"Could not start web dashboard: {e}")

    async def on_account_added_handler(data):
        if "token" in data and data["token"]:
            t = str(data["token"]).strip()
            task = asyncio.create_task(account_loop_token(t))
            bot_state.account_workers[t[:10]] = task
        elif "uid" in data and "password" in data:
            u = str(data["uid"]).strip()
            p = str(data["password"]).strip()
            task = asyncio.create_task(account_loop_guest(u, p))
            bot_state.account_workers[u] = task

    async def on_refresh_account_handler(uid):
        await refresh_account_profile(uid)

    bot_state.refresh_callbacks["on_account_added"] = on_account_added_handler
    bot_state.refresh_callbacks["on_refresh_account"] = on_refresh_account_handler

    accounts = load_accounts()

    if not accounts:
        print_warning(f"No accounts found in {ACCOUNTS_FILE}! Add accounts from Web Dashboard.")
        if PUBLIC_BASE_URL:
            print_warning(f"Open: {PUBLIC_BASE_URL}")
        else:
            print_warning(f"Open: http://localhost:{WEB_PORT}")

    for acc in accounts:
        if "token" in acc and acc["token"]:
            t = asyncio.create_task(account_loop_token(acc["token"]))
            bot_state.account_workers[acc["token"][:10]] = t
        elif "uid" in acc and "password" in acc and acc["uid"]:
            u = str(acc["uid"])
            t = asyncio.create_task(account_loop_guest(u, acc["password"]))
            bot_state.account_workers[u] = t

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError, SystemExit):
        print_warning("\n[STOP] Shutting down all accounts...")
        for t in list(bot_state.account_workers.values()):
            t.cancel()
        await asyncio.gather(*bot_state.account_workers.values(), return_exceptions=True)
        print_success("All sessions cleanly closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print_warning("\nProgram stopped by user.")