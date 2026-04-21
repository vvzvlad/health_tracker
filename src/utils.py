from datetime import timedelta, timezone


def parse_timezone(tz_str: str) -> timezone:
    sign = 1 if tz_str[0] == "+" else -1
    h, m = map(int, tz_str[1:].split(":"))
    return timezone(timedelta(hours=sign * h, minutes=sign * m))
