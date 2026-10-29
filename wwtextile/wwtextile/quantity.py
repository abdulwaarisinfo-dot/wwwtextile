"""Scaled-integer quantities.

Policy: every quantity is an integer number of thousandths of the item's unit
(3 decimal places). Floats are never used. Input with more than 3 decimals is
rejected rather than silently rounded.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

SCALE = 1000
MAX_QTY = 10**12  # 1 billion units; guards against absurd input


class QuantityError(ValueError):
    pass


def parse_qty(text: str | int | None, *, allow_zero: bool = False, field: str = "Quantity") -> int:
    """Parse user text such as '1,250.5' into thousandths. Raises QuantityError."""
    if text is None or (isinstance(text, str) and not text.strip()):
        raise QuantityError(f"{field} is required.")
    if isinstance(text, int):
        raise QuantityError(f"{field}: pass text, not a raw integer.")
    cleaned = text.strip().replace(",", "")
    try:
        d = Decimal(cleaned)
    except InvalidOperation:
        raise QuantityError(f"{field} '{text}' is not a number.") from None
    if not d.is_finite():
        raise QuantityError(f"{field} '{text}' is not a valid number.")
    scaled = d * SCALE
    if scaled != scaled.to_integral_value():
        raise QuantityError(f"{field} supports at most 3 decimal places.")
    value = int(scaled)
    if value < 0:
        raise QuantityError(f"{field} cannot be negative.")
    if value == 0 and not allow_zero:
        raise QuantityError(f"{field} must be greater than zero.")
    if value > MAX_QTY * SCALE:
        raise QuantityError(f"{field} is too large.")
    return value


def fmt_qty(value: int | None) -> str:
    """Format thousandths as '1,250.500'."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    whole, frac = divmod(abs(value), SCALE)
    return f"{sign}{whole:,}.{frac:03d}"
