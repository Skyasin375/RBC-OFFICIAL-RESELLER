#!/usr/bin/env python3
"""
Parse MajorLoginRes using the ProtoContract mapping.
Field definitions:
1: account_id (uint64)
2: lock_region (string)
3: noti_region (string)
4: ip_region (string)
5: agora_environment (string)
6: new_active_region (string)
7: recommend_regions (repeated string)
8: token (string)
9: ttl (uint32)
10: server_url (string)
12: emulator_score (uint32)
13: blacklist (nested BlacklistInfoRes)
15: queue_info (nested LoginQueueInfo)
16: tp_url (string)
17: app_server_id (uint32)
19: ip_city (string)
20: ip_subdivision (string)
21: kts (uint32)
22: ak (bytes)
23: aiv (bytes)
24: ffanti_url (string)
25: ff_anti_config_desc (nested FFAntiConfigDesc)
"""

import asyncio
import aiohttp
import ssl
import binascii
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# ========== Crypto (same as before) ==========
KEY = bytes([89, 103, 38, 116, 99, 37, 68, 69, 117, 104, 54, 37, 90, 99, 94, 56])
IV  = bytes([54, 111, 121, 90, 68, 114, 50, 50, 69, 51, 121, 99, 104, 106, 77, 37])

def aes_decrypt(data: bytes):
    cipher = AES.new(KEY, AES.MODE_CBC, IV)
    return unpad(cipher.decrypt(data), AES.block_size)

def aes_encrypt(data: bytes):
    cipher = AES.new(KEY, AES.MODE_CBC, IV)
    return cipher.encrypt(pad(data, AES.block_size))

# ========== Generic protobuf utilities ==========
def encode_varint(value: int) -> bytes:
    result = []
    while True:
        byte = value & 0x7F
        value >>= 7
        if value == 0:
            result.append(byte)
            break
        result.append(byte | 0x80)
    return bytes(result)

def decode_varint(data: bytes, offset: int):
    value = 0
    shift = 0
    while True:
        b = data[offset]
        value |= (b & 0x7F) << shift
        offset += 1
        shift += 7
        if not (b & 0x80):
            break
    return value, offset

def parse_fields(data: bytes):
    """Returns list of (field_num, wire_type, value) where value is raw for len-delimited."""
    fields = []
    idx = 0
    while idx < len(data):
        key, idx = decode_varint(data, idx)
        field_num = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:          # varint
            val, idx = decode_varint(data, idx)
            fields.append((field_num, 0, val))
        elif wire_type == 1:        # 64-bit
            val = int.from_bytes(data[idx:idx+8], 'little')
            idx += 8
            fields.append((field_num, 1, val))
        elif wire_type == 2:        # length-delimited
            length, idx = decode_varint(data, idx)
            raw = data[idx:idx+length]
            idx += length
            fields.append((field_num, 2, raw))
        elif wire_type == 5:        # 32-bit
            val = int.from_bytes(data[idx:idx+4], 'little')
            idx += 4
            fields.append((field_num, 5, val))
        else:
            raise ValueError(f"Unsupported wire type {wire_type}")
    return fields

def serialize_fields(fields):
    result = bytearray()
    for field_num, wire_type, value in fields:
        key = (field_num << 3) | wire_type
        result.extend(encode_varint(key))
        if wire_type == 0:
            result.extend(encode_varint(value))
        elif wire_type == 1:
            result.extend(value.to_bytes(8, 'little'))
        elif wire_type == 2:
            result.extend(encode_varint(len(value)))
            result.extend(value)
        elif wire_type == 5:
            result.extend(value.to_bytes(4, 'little'))
    return bytes(result)

def modify_string_field(fields, target_num, new_str):
    new_fields = []
    for fn, wt, val in fields:
        if fn == target_num and wt == 2:
            new_fields.append((fn, wt, new_str.encode('utf-8')))
        else:
            new_fields.append((fn, wt, val))
    return new_fields

# ========== Parse MajorLoginRes with mapping ==========
def parse_major_login_res(data: bytes) -> dict:
    fields = parse_fields(data)
    result = {}
    # We'll handle nested messages later – for now, store raw bytes for nested fields.
    for fn, wt, val in fields:
        if fn == 1 and wt == 0:
            result['account_id'] = val
        elif fn == 2 and wt == 2:
            result['lock_region'] = val.decode('utf-8')
        elif fn == 3 and wt == 2:
            result['noti_region'] = val.decode('utf-8')
        elif fn == 4 and wt == 2:
            result['ip_region'] = val.decode('utf-8')
        elif fn == 5 and wt == 2:
            result['agora_environment'] = val.decode('utf-8')
        elif fn == 6 and wt == 2:
            result['new_active_region'] = val.decode('utf-8')
        elif fn == 7 and wt == 2:
            # repeated string: each element is a length-delimited string inside a list
            # For simplicity, we parse the submessage as a list of strings
            sub_fields = parse_fields(val)
            regions = []
            for sub_fn, sub_wt, sub_val in sub_fields:
                if sub_wt == 2:
                    try:
                        regions.append(sub_val.decode('utf-8'))
                    except:
                        pass
            result['recommend_regions'] = regions
        elif fn == 8 and wt == 2:
            result['token'] = val.decode('utf-8')
        elif fn == 9 and wt == 0:
            result['ttl'] = val
        elif fn == 10 and wt == 2:
            result['server_url'] = val.decode('utf-8')
        elif fn == 12 and wt == 0:
            result['emulator_score'] = val
        elif fn == 13 and wt == 2:
            # Nested BlacklistInfoRes – we'll store raw for now
            result['blacklist'] = val.hex()  # or parse further if needed
        elif fn == 15 and wt == 2:
            # Nested LoginQueueInfo
            result['queue_info'] = val.hex()
        elif fn == 16 and wt == 2:
            result['tp_url'] = val.decode('utf-8')
        elif fn == 17 and wt == 0:
            result['app_server_id'] = val
        elif fn == 19 and wt == 2:
            result['ip_city'] = val.decode('utf-8')
        elif fn == 20 and wt == 2:
            result['ip_subdivision'] = val.decode('utf-8')
        elif fn == 21 and wt == 0:
            result['kts'] = val
        elif fn == 22 and wt == 2:
            result['ak'] = val.hex()
        elif fn == 23 and wt == 2:
            result['aiv'] = val.hex()
        elif fn == 24 and wt == 2:
            result['ffanti_url'] = val.decode('utf-8')
        elif fn == 25 and wt == 2:
            result['ff_anti_config_desc'] = val.hex()
    return result

# ========== Main flow (same as before, but with new parser) ==========
async def main():
    # Load and decrypt original payload
    with open("payload.txt", "r") as f:
        hex_data = "".join(f.read().strip().split())
    encrypted_orig = binascii.unhexlify(hex_data)
    print(f"[+] Loaded {len(encrypted_orig)} bytes")

    plain = aes_decrypt(encrypted_orig)
    print(f"[+] Decrypted {len(plain)} bytes")

    fields = parse_fields(plain)
    print(f"[+] Found {len(fields)} top-level fields")

    # Show current values
    for fn, wt, val in fields:
        if fn == 22 and wt == 2:
            try:
                print(f"  Field 22 (open_id) = {val.decode('utf-8')}")
            except:
                print(f"  Field 22 raw = {val.hex()}")
        if fn == 29 and wt == 2:
            try:
                token_str = val.decode('utf-8')
                print(f"  Field 29 (access_token) = {token_str[:40]}...")
            except:
                print(f"  Field 29 raw = {val.hex()}")

    NEW_OPEN_ID = "40372d879383f2deae398296c7e41c13"
    NEW_ACCESS_TOKEN = "672a3f11bd8ae81e450da632a645d1dee67d4a57221e4813cb90de7043123656"

    fields = modify_string_field(fields, 22, NEW_OPEN_ID)
    fields = modify_string_field(fields, 29, NEW_ACCESS_TOKEN)
    print("[+] Updated open_id and access_token")

    new_plain = serialize_fields(fields)
    new_encrypted = aes_encrypt(new_plain)
    print(f"[+] New encrypted payload size: {len(new_encrypted)} bytes")

    # Send request
    url = "https://loginbp.ggpolarbear.com/MajorLogin"
    headers = {
        "User-Agent": "GarenaMSDK/5.5.2P3(SM-A125F;Android 11;en-US;USA;)",
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Unity-Version": "2018.4.11f1",
        "X-GA": "v1 1",
        "ReleaseVersion": "OB53"
    }
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=new_encrypted, headers=headers, ssl=ssl_context) as resp:
            print(f"HTTP Status: {resp.status}")
            raw_response = await resp.read()
            print(f"Response size: {len(raw_response)} bytes")

            if resp.status != 200:
                print(f"Error response: {raw_response[:200]}")
                return

            # Parse response using the MajorLoginRes mapping (Skip 64-byte header)
            proto_payload = raw_response[64:] if len(raw_response) > 64 else raw_response
            parsed = parse_major_login_res(proto_payload)
            print("\n=== MajorLoginRes ===")
            for key, value in parsed.items():
                if key in ('ak', 'aiv', 'blacklist', 'queue_info', 'ff_anti_config_desc'):
                    print(f"{key:20} : {value} (hex)")
                elif key == 'recommend_regions':
                    print(f"{key:20} : {value}")
                else:
                    print(f"{key:20} : {value}")
            print("=====================")

if __name__ == "__main__":
    asyncio.run(main())