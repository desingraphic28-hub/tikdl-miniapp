"""
license.py  —  TikDL Bot key generation.
MUST be identical to the admin tool and TikDL app license.py.

Key format:  TIKDL-AAAAA-BBBBB-CCCCC-DDDDD-EEEEE-FFFFF
  Groups:    PREFIX - MID_HASH(5) - DAYS(5) - DATE(5) - R1(5) - R2(5) - CHECK(5)
"""

import hmac
import hashlib
import secrets
import datetime

import config

KEY_PREFIX   = "TIKDL"
KEY_PART_LEN = 5
CHARS        = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

SECRET_KEY = config.SECRET_KEY


# ── Base-32 helpers ────────────────────────────────────────────────────────────

def _enc32(n: int, width: int) -> str:
    result = []
    for _ in range(width):
        result.append(CHARS[n % len(CHARS)])
        n //= len(CHARS)
    return "".join(reversed(result))


def _random_group() -> str:
    return "".join(secrets.choice(CHARS) for _ in range(KEY_PART_LEN))


def _mid_group(machine_id: str) -> str:
    h = hashlib.sha256((SECRET_KEY + machine_id.encode()).hex().encode()).hexdigest()
    return "".join(CHARS[int(h[i*2:i*2+2], 16) % len(CHARS)] for i in range(KEY_PART_LEN))


def _hmac_check(payload: str) -> str:
    sig = hmac.new(SECRET_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return "".join(CHARS[int(sig[i*2:i*2+2], 16) % len(CHARS)] for i in range(KEY_PART_LEN))


def _today_days() -> int:
    return (datetime.datetime.utcnow().date() - datetime.date(1970, 1, 1)).days


# ── Key generation ─────────────────────────────────────────────────────────────

def generate_key(machine_id: str, days: int) -> str:
    mid_grp  = _mid_group(machine_id)
    days_grp = _enc32(days, KEY_PART_LEN)
    date_grp = _enc32(_today_days(), KEY_PART_LEN)
    r1, r2   = _random_group(), _random_group()
    payload  = f"{KEY_PREFIX}-{mid_grp}-{days_grp}-{date_grp}-{r1}-{r2}"
    return f"{payload}-{_hmac_check(payload)}"
