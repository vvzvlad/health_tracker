from datetime import timedelta, timezone
from loguru import logger


def parse_timezone(tz_str: str) -> timezone:
    try:
        if not tz_str or tz_str[0] not in ('+', '-'):
            raise ValueError(f"Invalid timezone: {tz_str!r}")
        sign = 1 if tz_str[0] == '+' else -1
        h, m = map(int, tz_str[1:].split(':'))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"Out of range: {tz_str!r}")
        return timezone(timedelta(hours=sign * h, minutes=sign * m))
    except Exception:
        logger.warning("Invalid timezone string {!r}, falling back to UTC", tz_str)
        return timezone.utc
