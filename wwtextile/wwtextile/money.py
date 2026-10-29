"""Money is INTEGER paisa (minor units); floats are never used. Currency is configuration (WW_CURRENCY)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

MAX_PAISE = 10**15  # 10 trillion rupees; guards absurd input


class MoneyError(ValueError):
    pass


def parse_money(text, *, field: str = "Amount") -> int:
    if text is None or not str(text).strip():
        raise MoneyError(f"{field} is required.")
    cleaned = str(text).strip().replace(",", "")
    try:
        d = Decimal(cleaned)
    except InvalidOperation:
        raise MoneyError(f"{field} '{text}' is not a number.") from None
    if not d.is_finite():
        raise MoneyError(f"{field} '{text}' is not a valid number.")
    scaled = d * 100
    if scaled != scaled.to_integral_value():
        raise MoneyError(f"{field} supports at most 2 decimal places.")
    v = int(scaled)
    if v <= 0:
        raise MoneyError(f"{field} must be greater than zero.")
    if v > MAX_PAISE:
        raise MoneyError(f"{field} is too large.")
    return v


def fmt_money(paise: int | None, currency: str = "") -> str:
    if paise is None:
        return "—"
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    return f"{sign}{currency + ' ' if currency else ''}{whole:,}.{frac:02d}"
