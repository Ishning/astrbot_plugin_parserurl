"""Minimal pure-Python helpers for Douyin Web request parameters."""

import hashlib
import random
import time
from urllib.parse import quote


_SALT = "dhzx"
_ALPHA3 = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe"
_ALPHA4 = "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe"
_PERM = (34, 44, 56, 61, 73, 29, 70, 45, 35, 49, 38, 66, 51, 68, 28, 48, 64, 47,
         30, 71, 26, 55, 31, 69, 59, 40, 62, 63, 27, 72, 41, 74, 57, 52, 42, 39,
         33, 67, 53, 43, 65, 46, 36, 24, 60, 32, 79, 80, 84, 85)
_CHECKSUM_KEYS = (24, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 38, 39, 40,
                  41, 42, 43, 44, 45, 46, 47, 48, 49, 51, 52, 53, 55, 56, 57,
                  59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73,
                  74, 79, 80, 84, 85)
_GEO = (1920, 937, 1920, 1040, 1920, 1040, 1920, 1080)


def generate_ms_token(length: int = 107) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789="
    return "".join(random.choice(alphabet) for _ in range(length))


def generate_web_id() -> str:
    return "".join(random.choice("0123456789") for _ in range(19))


def _splice(params: dict[str, str]) -> str:
    return "&".join(f"{key}={quote(str(value))}" for key, value in params.items())


def _sm3(value: bytes) -> bytes:
    return hashlib.new("sm3", value).digest()


def _b64(data: list[int], alphabet: str) -> str:
    output: list[str] = []
    for index in range(0, len(data), 3):
        block = data[index:index + 3] + [0, 0]
        value = (block[0] << 16) | (block[1] << 8) | block[2]
        chars = [alphabet[(value >> shift) & 63] for shift in (18, 12, 6, 0)]
        remaining = len(data) - index
        output.extend(chars if remaining >= 3 else chars[:remaining + 1] + ["="] * (3 - remaining))
    return "".join(output)


def _rc4(key: list[int], data: list[int]) -> list[int]:
    box = [255 - item for item in range(256)]
    j = 0
    for index in range(256):
        j = (j * box[index] + j + key[index % len(key)]) % 256
        box[index], box[j] = box[j], box[index]
    index = j = 0
    output = []
    for value in data:
        index = (index + 1) % 256
        j = (j + box[index]) % 256
        box[index], box[j] = box[j], box[index]
        output.append(value ^ box[(box[index] + box[j]) % 256])
    return output


def _bytes(value: int, count: int) -> list[int]:
    return [(value >> (8 * index)) & 255 for index in range(count)]


def _text_bytes(value: str) -> list[int]:
    output = []
    for char in value:
        code = ord(char)
        output.extend((code >> 8, code & 255) if code & 0xFF00 else (code,))
    return output


def _blend(first: int, second: int, random_first: int, random_second: int) -> list[int]:
    return [
        (random_first & 170) | (first & 85), (random_first & 85) | (first & 170),
        (random_second & 170) | (second & 85), (random_second & 85) | (second & 170),
    ]


def _escape_digest(digest: bytes, index: int, reserved: int, fallback: int, force: bool) -> int:
    value = digest[index] if index < len(digest) else fallback
    while value == reserved:
        index += 1
        value = digest[index] if index < len(digest) else fallback
    return reserved if force else value


def _expand(values: list[int]) -> list[int]:
    output = []
    for index in range(0, len(values), 3):
        if index + 2 >= len(values):
            output.append(values[index])
            if index + 1 < len(values) and values[index + 1]:
                output.append(values[index + 1])
            continue
        random_byte = int(random.random() * 1000) & 255
        first, second, third = values[index:index + 3]
        output.extend((
            (random_byte & 145) | (first & 110),
            (random_byte & 66) | (second & 189),
            (random_byte & 44) | (third & 211),
            (first & 145) | (second & 66) | (third & 44),
        ))
    return output


class ABogusSigner:
    """Port of the reference project's pure a_bogus signer."""

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self.counter = 2

    def sign_params(self, params: dict[str, str]) -> str:
        query = _splice(params)
        self.counter += 1
        now = int(time.time() * 1000)
        first_hash = _sm3(_sm3((query + _SALT).encode()))
        empty_hash = _sm3(_sm3(_SALT.encode()))
        ua_hash = _sm3(_b64(_rc4([0, 129, 14], [ord(char) for char in self.user_agent]), _ALPHA3).encode())
        flags = 3
        data: dict[int, int | list[int]] = {12: 3, 24: 41, 25: [1, 0, 1, 0, 1]}
        data[26] = int((now - 1721836800000) / 1209600000)
        data[27] = 6 if self.counter <= 139 else 5
        data[28] = 3
        for index, value in enumerate(_bytes(now, 6)):
            data[29 + index] = value
        data[35], data[36] = 129, 0
        data[38], data[39] = flags, 0
        for index in range(40, 44):
            data[index] = 0
        data[44], data[45], data[46], data[47] = 14, 0, 0, 0
        data[48], data[49] = first_hash[9], first_hash[18]
        data[51] = _escape_digest(first_hash, 3, 11, 12, True)
        data[52], data[53] = empty_hash[10], empty_hash[19]
        data[55] = _escape_digest(empty_hash, 4, 8, 9, False)
        data[56], data[57] = ua_hash[11], ua_hash[21]
        data[59] = _escape_digest(ua_hash, 5, 12, 13, False)
        for index, value in enumerate(_bytes(now - 1, 6)):
            data[60 + index] = value
        data[66] = 3
        for index, value in enumerate(_bytes(6383, 4)):
            data[67 + index] = value
            data[71 + index] = value
        geo = _text_bytes("|".join(map(str, _GEO)) + "|Win32")
        data[77], data[78] = geo, len(geo)
        data[79], data[80] = _bytes(len(geo), 2)
        tail = _text_bytes(f"{(now + 3) & 255},")
        data[82], data[83] = tail, len(tail)
        data[84], data[85] = _bytes(len(tail), 2)
        value = int(random.random() * 65535)
        block = _blend(1, 0, value & 255, value >> 8)
        random.random()
        check_value = int(random.random() * 240)
        check_value = check_value + (check_value % 2) + 1 if check_value > 109 else check_value
        permission_value = (int(random.random() * 255) & 77) | 2 | 16 | 32 | 128
        block += _blend(1, 0, check_value, permission_value)
        checksum = 0
        for item in block + [data[key] for key in _CHECKSUM_KEYS]:
            checksum ^= item
        payload = [data[key] for key in _PERM] + geo + tail + [checksum & 255]
        prefix = _blend(3, 82, int(random.random() * 65535) & 255, int(random.random() * 40))
        encrypted = _rc4([211], _expand(payload))
        return _b64(prefix + encrypted, _ALPHA4)
