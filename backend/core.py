"""
Dependency-free helpers for Ruchira POS (standard library only).

Everything here is deliberately free of FastAPI / psycopg so it can be unit-tested anywhere
(see tests/test_core.py). backend/app.py wires these into the web routes.

Contents
  1. Order types and money validation
  2. India-local business time <-> UTC storage
  3. Login throttling and client-IP extraction
  4. Role / permission matrix (single source of truth)
  5. Password policy
  6. Static-file allow-list and security-header builders
"""
from __future__ import annotations

import ipaddress
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Optional

# ---------------------------------------------------------------------------------------------
# 1. Order types and money
# ---------------------------------------------------------------------------------------------
ORDER_TYPES = ('Pickup', 'Delivery')
_ORDER_TYPE_BY_LOWER = {t.lower(): t for t in ORDER_TYPES}


def normalize_order_type(value) -> str:
    """'delivery' / ' Delivery ' -> 'Delivery'. Anything that is not a known order type raises ValueError."""
    if not isinstance(value, str):
        raise ValueError('Order type must be text: Pickup or Delivery.')
    canonical = _ORDER_TYPE_BY_LOWER.get(value.strip().lower())
    if canonical is None:
        raise ValueError('Order type must be Pickup or Delivery.')
    return canonical


MONEY_MAX = Decimal('1000000')   # a single delivery fee / price above 10 lakh is always a typo


def parse_money(value, field: str = 'Amount', *, maximum: Decimal = MONEY_MAX, allow_zero: bool = True) -> Decimal:
    """Validate a rupee amount and return it as a Decimal with two decimal places.

    Rejects booleans, NaN/Infinity, negatives, more than the maximum, and text that is not a number.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f'{field} must be a number.')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f'{field} must be a finite number.')
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f'{field} must be a number.')
    if not d.is_finite():
        raise ValueError(f'{field} must be a finite number.')
    if d < 0:
        raise ValueError(f'{field} cannot be negative.')
    if d == 0 and not allow_zero:
        raise ValueError(f'{field} must be more than zero.')
    if d > maximum:
        raise ValueError(f'{field} is too large.')
    return d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def money_float(d: Decimal) -> float:
    return float(d)


def compute_totals(prices_and_qty: Iterable[tuple], tax_percent, delivery_fee) -> dict:
    """subtotal / tax / total from (unit_price, quantity) pairs, using Decimal so cents never drift."""
    subtotal = sum((Decimal(str(p)) * int(q) for p, q in prices_and_qty), Decimal('0'))
    tax = (subtotal * Decimal(str(tax_percent or 0)) / Decimal('100'))
    delivery = Decimal(str(delivery_fee or 0))
    if delivery < 0:
        raise ValueError('Delivery fee cannot be negative.')
    q = Decimal('0.01')
    subtotal, tax, delivery = (x.quantize(q, rounding=ROUND_HALF_UP) for x in (subtotal, tax, delivery))
    return {'subtotal': subtotal, 'tax': tax, 'delivery_fee': delivery, 'total': subtotal + tax + delivery}


# ---------------------------------------------------------------------------------------------
# 2. Time: store UTC, report in India time
# ---------------------------------------------------------------------------------------------
# India has no daylight saving time, so a fixed +05:30 offset is exact and needs no tz database
# (slim Docker images often have none, and zoneinfo would then fail).
IST = timezone(timedelta(hours=5, minutes=30), 'Asia/Kolkata')
UTC = timezone.utc
MONTH_RE = re.compile(r'^(\d{4})-(\d{2})$')


def utc_now() -> datetime:
    return datetime.now(UTC)


def ist_now() -> datetime:
    return utc_now().astimezone(IST)


def ist_today() -> date:
    return ist_now().date()


def current_month_ist() -> str:
    n = ist_now()
    return f'{n.year:04d}-{n.month:02d}'


def db_timestamp(dt: datetime) -> datetime:
    """The value to hand to the database for a timestamp column.

    Existing rows hold naive UTC clock values (the hosted server runs in UTC), and the column may be
    `timestamp` or `timestamptz`. A naive-UTC value is correct for both when the session time zone is
    UTC (Supabase's default), so aware datetimes are converted to naive UTC only at this one boundary.
    """
    if dt.tzinfo is None:
        raise ValueError('Refusing to store a naive datetime: attach a time zone first.')
    return dt.astimezone(UTC).replace(tzinfo=None)


def parse_month(month: Optional[str]) -> tuple[int, int]:
    """'2026-03' -> (2026, 3). None -> the current India-local month. Invalid text raises ValueError."""
    if not month:
        month = current_month_ist()
    m = MONTH_RE.match(month)
    if not m:
        raise ValueError('Month must look like 2026-03.')
    year, mon = int(m.group(1)), int(m.group(2))
    if not (2000 <= year <= 2100 and 1 <= mon <= 12):
        raise ValueError('Month is out of range.')
    return year, mon


def month_bounds_utc(month: Optional[str]) -> tuple[datetime, datetime]:
    """[start, end) of an India-local calendar month, as naive-UTC datetimes for the database.

    An order placed at 02:00 IST on 1 April is 20:30 UTC on 31 March: it belongs to April's report.
    """
    year, mon = parse_month(month)
    start_ist = datetime(year, mon, 1, tzinfo=IST)
    end_ist = datetime(year + (mon == 12), 1 if mon == 12 else mon + 1, 1, tzinfo=IST)
    return db_timestamp(start_ist), db_timestamp(end_ist)


def day_bounds_utc(day: Optional[date] = None) -> tuple[datetime, datetime]:
    """[start, end) of one India-local calendar day (default: today in India), as naive-UTC datetimes."""
    day = day or ist_today()
    start_ist = datetime(day.year, day.month, day.day, tzinfo=IST)
    return db_timestamp(start_ist), db_timestamp(start_ist + timedelta(days=1))


def ist_business_date(stored_naive_utc: datetime) -> date:
    """The India-local business date of a stored (naive UTC) timestamp."""
    return stored_naive_utc.replace(tzinfo=UTC).astimezone(IST).date()


# ---------------------------------------------------------------------------------------------
# 3. Client IP and login throttling
# ---------------------------------------------------------------------------------------------
def client_ip(forwarded_for: str, peer: str, trusted_hops: int) -> str:
    """The address to rate-limit on.

    `trusted_hops` is the number of proxies you control in front of the app (Render = 1). The proxy
    APPENDS the address it received the request from, so the real client is the Nth entry from the RIGHT;
    everything to its left was supplied by the client and may be forged.
    trusted_hops=0 means "no proxy": X-Forwarded-For is ignored completely.
    Malformed values fall back to the socket peer, so garbage can never create a fresh bucket per request.
    """
    fallback = _normalise_ip(peer) or 'unknown'
    if trusted_hops <= 0:
        return fallback
    hops = [h.strip() for h in (forwarded_for or '').split(',') if h.strip()]
    if len(hops) < trusted_hops:
        return fallback
    return _normalise_ip(hops[-trusted_hops]) or fallback


def _normalise_ip(text: str) -> Optional[str]:
    try:
        ip = ipaddress.ip_address((text or '').strip().strip('[]'))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        # one household / ISP customer normally owns a whole /64, so rotating inside it must not reset the limit
        return str(ipaddress.ip_network(f'{ip}/64', strict=False).network_address) + '/64'
    return str(ip)


class LoginThrottle:
    """Bounded in-memory failed-login limiter that cannot be used to lock the owner out.

    Two hard limits, both keyed on the CLIENT ADDRESS, so a stranger can only ever block themselves:
      * per (address, username): `pair_limit` failures in `window` seconds
      * per address (any username): `ip_limit` failures in `window` seconds
    Failures per username across all addresses are only counted (see `username_failures`) so the
    server can log an alert; they never block anybody. Passwords are still verified at normal speed
    for everyone who is not rate-limited.

    State lives in this process. With several workers / instances each has its own counters, so the
    effective limit is multiplied by the worker count. Put a proxy-level limit in front for more.
    """

    def __init__(self, pair_limit=6, ip_limit=30, window=600, max_keys=20000, clock=time.time):
        self.pair_limit, self.ip_limit, self.window, self.max_keys, self._clock = pair_limit, ip_limit, window, max_keys, clock
        self._pair: dict[str, list[float]] = {}
        self._ip: dict[str, list[float]] = {}
        self._user: dict[str, list[float]] = {}

    @staticmethod
    def _key_user(username: str) -> str:
        return (username or '').strip().lower()[:64]

    def _recent(self, store: dict, key: str) -> list[float]:
        now = self._clock()
        hits = [t for t in store.get(key, ()) if now - t < self.window]
        if hits:
            store[key] = hits
        else:
            store.pop(key, None)
        return hits

    def blocked(self, ip: str, username: str) -> bool:
        u = self._key_user(username)
        return len(self._recent(self._pair, f'{ip}|{u}')) >= self.pair_limit or len(self._recent(self._ip, ip)) >= self.ip_limit

    def record_failure(self, ip: str, username: str) -> None:
        u, now = self._key_user(username), self._clock()
        for store, key in ((self._pair, f'{ip}|{u}'), (self._ip, ip), (self._user, u)):
            store.setdefault(key, []).append(now)
        for store in (self._pair, self._ip, self._user):
            if len(store) > self.max_keys:   # memory bound: drop the stalest half
                for k in sorted(store, key=lambda k: store[k][-1])[: len(store) // 2]:
                    store.pop(k, None)

    def record_success(self, ip: str, username: str) -> None:
        self._pair.pop(f'{ip}|{self._key_user(username)}', None)

    def username_failures(self, username: str) -> int:
        return len(self._recent(self._user, self._key_user(username)))


# ---------------------------------------------------------------------------------------------
# 4. Role / permission matrix: ONE table, enforced centrally by the middleware
# ---------------------------------------------------------------------------------------------
PUBLIC_PATHS = frozenset({'/api/login'})
# Allowed even while a password change is pending (everything else is refused with 403 password_change_required):
PASSWORD_CHANGE_PATHS = frozenset({'/api/me', '/api/me/password', '/api/logout'})

# (methods, path regex) that a STAFF login may call. Every other /api route is OWNER-only (deny by default).
_ID = r'[0-9]{1,18}'
STAFF_ALLOWED: tuple[tuple[frozenset, re.Pattern], ...] = tuple(
    (frozenset(methods.split()), re.compile(pattern))
    for methods, pattern in (
        ('GET', r'/api/me'),
        ('POST', r'/api/me/password'),
        ('POST', r'/api/logout'),
        ('GET', r'/api/settings'),
        ('GET', r'/api/categories'),
        ('GET', r'/api/menu-items'),
        ('PUT', rf'/api/menu-items/{_ID}'),          # only the "available" switch is accepted for staff (checked in the route)
        ('GET', r'/api/next-order-number'),
        ('GET POST', r'/api/orders'),
        ('GET PUT', rf'/api/orders/{_ID}'),           # NOT delete
        ('GET', rf'/api/print/{_ID}'),
        ('POST', r'/api/printers/print'),
        ('POST', r'/api/printers/print-batch'),
        ('GET', r'/api/printers'),
        ('GET', r'/api/tables'),
        ('POST', rf'/api/tables/{_ID}/free'),         # staff can free a table; adding / removing tables is owner-only
    )
)

# Documented for the report / tests; the code path above is what actually enforces it.
OWNER_ONLY_DOC = (
    'POST/PUT/DELETE /api/categories*', 'POST/DELETE /api/menu-items*', 'DELETE /api/orders/{id}', 'POST /api/orders/clear',
    'GET /api/reports', '* /api/inventory*', '* /api/users*', 'PUT /api/settings', 'POST /api/printers/test', 'POST /api/demo/reset',
    'GET /docs, /redoc, /openapi.json (when enabled)',
)


def normalise_path(path: str) -> str:
    p = (path or '/')
    p = re.sub(r'/{2,}', '/', p)
    return (p.rstrip('/') or '/')


def required_role(method: str, path: str) -> str:
    """'public' | 'staff' | 'owner' for an API request. Unknown routes default to 'owner' (least privilege)."""
    path = normalise_path(path)
    if path in PUBLIC_PATHS:
        return 'public'
    method = method.upper()
    for methods, pattern in STAFF_ALLOWED:
        if method in methods and pattern.fullmatch(path):
            return 'staff'
    return 'owner'


# ---------------------------------------------------------------------------------------------
# 5. Password policy
# ---------------------------------------------------------------------------------------------
MIN_PASSWORD = 10
MAX_PASSWORD = 128
KNOWN_DEFAULT_PASSWORDS = ('admin123',)   # a login that still accepts one of these is forced to change it
_COMMON = frozenset({
    'admin123', 'admin1234', 'administrator', 'password', 'password1', 'password123', 'passw0rd', 'letmein', 'welcome', 'welcome1',
    'welcome123', 'qwerty', 'qwerty123', 'qwertyuiop', 'abc123', 'abcd1234', '123456', '1234567', '12345678', '123456789',
    '1234567890', '0123456789', '111111', '000000', 'iloveyou', 'changeme', 'changeme123', 'ruchira', 'ruchira123', 'ruchirapos',
    'demokitchen', 'kitchen123', 'restaurant', 'restaurant123', 'pos12345', 'india123', 'india@123',
})


def password_problem(password: str, username: str = '') -> Optional[str]:
    """None if acceptable, otherwise a short message that is safe to show the user."""
    if not isinstance(password, str):
        return 'The password must be text.'
    if len(password) < MIN_PASSWORD:
        return f'The password must be at least {MIN_PASSWORD} characters.'
    if len(password) > MAX_PASSWORD:
        return f'The password must be at most {MAX_PASSWORD} characters.'
    low = password.strip().lower()
    if low in _COMMON or any(low == d for d in KNOWN_DEFAULT_PASSWORDS):
        return 'That password is too common. Please choose a less guessable one.'
    if username and low == username.strip().lower():
        return 'The password must not be the same as the username.'
    if len(set(password)) < 4:
        return 'That password is too repetitive. Please choose a less guessable one.'
    return None


# ---------------------------------------------------------------------------------------------
# 6. Static files and security headers
# ---------------------------------------------------------------------------------------------
STATIC_EXTENSIONS = frozenset({'.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.woff', '.woff2', '.ttf'})
_SAFE_SEGMENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def static_path_allowed(rel: str) -> bool:
    """True only for plain asset files: no dotfiles, no '..', no documentation / config / source maps."""
    if not rel or len(rel) > 200 or '\\' in rel or '\x00' in rel:
        return False
    parts = rel.split('/')
    if any(p in ('', '.', '..') or not _SAFE_SEGMENT.match(p) for p in parts):
        return False
    dot = parts[-1].rfind('.')
    return dot > 0 and parts[-1][dot:].lower() in STATIC_EXTENSIONS


def build_csp(strict: bool) -> str:
    """strict=True: scripts only from this site, no inline script handlers. strict=False: compatible baseline."""
    script = "script-src 'self'" if strict else "script-src 'self' 'unsafe-inline'"
    return '; '.join((
        "default-src 'self'",
        script,
        "style-src 'self' 'unsafe-inline'",     # the UI uses style="" attributes and print popups carry <style>
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ))


def security_headers(*, https: bool, csp_mode: str) -> dict:
    """csp_mode: 'enforce' (strict policy enforced) | 'report-only' (default: baseline enforced, strict reported in the
    browser console) | 'baseline' (baseline only) | 'off'."""
    h = {
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'same-origin',
        'Cross-Origin-Opener-Policy': 'same-origin',
        'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=(), usb=()',
    }
    if https:
        h['Strict-Transport-Security'] = 'max-age=15552000'
    if csp_mode == 'enforce':
        h['Content-Security-Policy'] = build_csp(True)
    elif csp_mode == 'report-only':
        h['Content-Security-Policy'] = build_csp(False)
        h['Content-Security-Policy-Report-Only'] = build_csp(True)
    elif csp_mode == 'baseline':
        h['Content-Security-Policy'] = build_csp(False)
    return h
