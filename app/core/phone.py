"""Sri Lankan mobile numbers, stored as +947XXXXXXXX.

Accepts what people type: 0715810938, 715810938, 94715810938, +94 71 581 0938, 071-581-0938.
"""

import re

_DIGITS = re.compile(r"\D")


class InvalidPhone(ValueError):
    pass


def normalise_mobile(raw: str) -> str:
    digits = _DIGITS.sub("", raw or "")
    if digits.startswith("94"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 9 or not digits.startswith("7"):
        raise InvalidPhone("enter a Sri Lankan mobile number, e.g. 0715810938")
    return f"+94{digits}"


def mask(phone: str) -> str:
    """+94715810938 -> +94 71 *** *938 (for messages and logs)."""
    return f"{phone[:3]} {phone[3:5]} *** *{phone[-3:]}" if len(phone) == 12 else "***"
