from datetime import datetime, timedelta, timezone
from pathlib import Path
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import textwrap
import threading
import time
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator
from dotenv import load_dotenv
from psycopg import connect, errors
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

try:                      # started as  uvicorn backend.app:app   (project root)
    from . import core
except ImportError:       # started as  uvicorn app:app           (from inside backend/)
    import core

log = logging.getLogger('ruchira')
audit_log = logging.getLogger('ruchira.audit')
if not logging.getLogger().handlers:
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(), format='%(asctime)s %(levelname)s %(name)s: %(message)s')

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
load_dotenv(ROOT / '.env')
DB_URL = os.getenv('DATABASE_URL', '').strip()
STATIC = ROOT / 'frontend'


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


# APP_ENV decides how strict the server is. Anything that is not clearly a development setting is PRODUCTION.
APP_ENV = (os.getenv('APP_ENV') or 'production').strip().lower()
PRODUCTION = APP_ENV not in ('development', 'dev', 'local', 'test', 'testing')
# Demo data and the destructive demo reset exist only outside production, and only when explicitly switched on.
DEMO_ENABLED = env_flag('SEED_DEMO_DATA', False) and not PRODUCTION
if env_flag('SEED_DEMO_DATA', False) and PRODUCTION:
    log.warning('SEED_DEMO_DATA is set but APP_ENV is production: demo data and /api/demo/reset stay DISABLED.')
# Number of reverse proxies you control in front of the app (Render = 1). 0 = none: X-Forwarded-For is ignored.
_hops_raw = (os.getenv('TRUSTED_PROXY_HOPS') or '1').strip()
TRUSTED_PROXY_HOPS = int(_hops_raw) if _hops_raw.isdigit() else 1
CSP_MODE = (os.getenv('CSP_MODE') or 'report-only').strip().lower()      # enforce | report-only | baseline | off
if CSP_MODE not in ('enforce', 'report-only', 'baseline', 'off'):
    CSP_MODE = 'report-only'
MAX_BODY_BYTES = int(os.getenv('MAX_BODY_BYTES', str(256 * 1024)))
AUTH_SCHEMA = (os.getenv('AUTH_SCHEMA') or 'public').strip().lower()      # 'public' (default) or 'private' after migration 0004
if not re.fullmatch(r'[a-z_][a-z0-9_]{0,40}', AUTH_SCHEMA):
    raise RuntimeError('AUTH_SCHEMA must be a plain lower-case identifier such as public or private.')
T_USERS, T_SESSIONS = f'{AUTH_SCHEMA}.users', f'{AUTH_SCHEMA}.sessions'

_docs_on = os.getenv('ENABLE_DOCS', '').strip().lower() in ('1', 'true', 'yes', 'on')   # API docs are OFF unless asked for (and owner-only when on)
app = FastAPI(title='Demo Kitchen API', version='0.2.0',
              docs_url='/docs' if _docs_on else None, redoc_url='/redoc' if _docs_on else None, openapi_url='/openapi.json' if _docs_on else None)
_cors_origins = [o.strip() for o in os.getenv('CORS_ORIGINS', '').split(',') if o.strip()]   # the app is same-origin, so no CORS unless listed
if _cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, allow_methods=['GET', 'POST', 'PUT', 'DELETE'], allow_headers=['Content-Type'])

app.add_middleware(GZipMiddleware, minimum_size=1200)   # orders / app.js download several times smaller

STATUSES = ['New', 'Accepted', 'Preparing', 'Ready', 'Completed', 'Cancelled']


class MenuItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(max_length=80)
    price: float = Field(gt=0, le=100000, allow_inf_nan=False)
    available: bool = True


class CategoryIn(BaseModel):
    name: str = Field(max_length=80)
    icon: str = Field(default='', max_length=16)


class CategoryOrderIn(BaseModel):
    category_ids: list[int] = Field(max_length=500)


class OrderItemIn(BaseModel):
    menu_item_id: int = Field(ge=1, le=2_000_000_000)
    quantity: int = Field(ge=1, le=1000)


def _order_type(v):
    try:
        return core.normalize_order_type(v)
    except ValueError as exc:
        raise ValueError(str(exc))


class OrderIn(BaseModel):
    customer_name: str = Field(default='', max_length=120)   # optional: walk-in orders have no name
    phone: str = Field(default='', max_length=32)            # optional
    address: str = Field(default='', max_length=300)
    table_number: str = Field(default='', max_length=20)     # optional: which table this bill is for
    order_type: str = 'Pickup'
    items: list[OrderItemIn] = Field(min_length=1, max_length=100)
    special_instructions: str = Field(default='', max_length=500)
    payment_method: str = Field(default='Cash', max_length=20)
    payment_status: str = Field(default='Pending', max_length=20)
    delivery_fee: float = Field(default=0, ge=0, le=100000, allow_inf_nan=False)
    auto_print: bool = False
    # Kept so older screens still work, but it is only a HINT: the server always assigns the real number itself.
    order_number: Optional[int] = Field(default=None, ge=0, le=2_000_000_000)
    # One value per "Confirm" press. A retry after a lost reply sends the same value, so it can never create a second order.
    client_request_id: Optional[str] = Field(default=None, pattern=r'^[A-Za-z0-9_-]{8,64}$')

    @field_validator('order_type')
    @classmethod
    def _check_type(cls, v):
        return _order_type(v)


class TableIn(BaseModel):
    label: str = Field(default='', max_length=20)   # empty = next number (7, 8, ...)


class OrderUpdateIn(BaseModel):
    """Everything is optional: only the fields that are sent are changed."""
    customer_name: Optional[str] = Field(default=None, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=32)
    address: Optional[str] = Field(default=None, max_length=300)
    order_type: Optional[str] = None
    payment_status: Optional[Literal['Paid', 'Pending']] = None
    payment_method: Optional[str] = Field(default=None, max_length=20)
    special_instructions: Optional[str] = Field(default=None, max_length=500)
    status: Optional[Literal['New', 'Accepted', 'Preparing', 'Ready', 'Completed', 'Cancelled']] = None
    items: Optional[list[OrderItemIn]] = Field(default=None, max_length=100)
    delivery_fee: Optional[float] = Field(default=None, ge=0, le=100000, allow_inf_nan=False)

    @field_validator('order_type')
    @classmethod
    def _check_type(cls, v):
        return None if v is None else _order_type(v)


class SettingsIn(BaseModel):
    business_name: str = Field(max_length=120)
    address: str = Field(default='', max_length=300)
    phone: str = Field(default='', max_length=32)
    gst_number: str = Field(default='', max_length=32)
    food_license: str = Field(default='', max_length=32)
    printer_name: str = Field(default='58mm Mini Thermal Printer', max_length=120)
    paper_size: str = Field(default='58mm', max_length=10)
    auto_print: bool = False
    default_order_type: str = 'Pickup'
    tax_percent: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    delivery_fee: float = Field(default=0, ge=0, le=100000, allow_inf_nan=False)
    printer_mode: Literal['browser', 'windows', 'serial'] = 'browser'
    printer_target: str = Field(default='', max_length=200)
    printer_baudrate: int = Field(default=9600, ge=300, le=921600)

    @field_validator('default_order_type')
    @classmethod
    def _check_type(cls, v):
        return _order_type(v)


class InventoryPurchaseIn(BaseModel):
    item_name: str = Field(min_length=1, max_length=120)
    category: str = Field(default='Raw Material', max_length=60)
    quantity: float = Field(default=1, gt=0, le=1_000_000, allow_inf_nan=False)
    unit: str = Field(default='batch', max_length=20)
    unit_price: float = Field(default=0, ge=0, le=10_000_000, allow_inf_nan=False)
    amount: Optional[float] = Field(default=None, ge=0, le=10_000_000, allow_inf_nan=False)   # total paid. When given it wins over quantity x unit price. The upper limits stop a typo from breaking the totals
    purchase_date: str = Field(default='', max_length=10)
    notes: str = Field(default='', max_length=300)


def purchase_numbers(p):
    """(unit_price, total_cost) for a purchase. Just typing the amount paid is enough."""
    if p.amount is not None:
        total = round(p.amount, 2)
        return round(total / p.quantity, 2), total
    return p.unit_price, round(p.quantity * p.unit_price, 2)


def clean_purchase_date(value: str) -> str:
    """A purchase date must be a real calendar date (YYYY-MM-DD). Anything else gets a clear message instead of a server error."""
    raw = (value or '').strip()
    if not raw:
        return core.ist_today().isoformat()
    try:
        d = datetime.strptime(raw[:10], '%Y-%m-%d').date()
    except ValueError:
        raise HTTPException(400, 'The purchase date is not valid. Please pick the date again.')
    if not 2000 <= d.year <= 2100:
        raise HTTPException(400, 'The purchase date is not valid. Please check the year.')
    return d.isoformat()


class LoginIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)   # bounded: scrypt cost must not be controllable by the caller


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=core.MAX_PASSWORD)


class UserCreateIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(max_length=64)
    name: str = Field(default='', max_length=80)
    password: str = Field(max_length=core.MAX_PASSWORD)
    role: Literal['owner', 'staff'] = 'staff'


class UserUpdateIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Optional[str] = Field(default=None, max_length=80)
    role: Optional[Literal['owner', 'staff']] = None
    active: Optional[bool] = None
    password: Optional[str] = Field(default=None, max_length=core.MAX_PASSWORD)


class HardwarePrintIn(BaseModel):
    order_id: int = Field(ge=1, le=2_000_000_000)
    target: str = Field(default='', max_length=200)
    mode: str = Field(default='', max_length=20)


class HardwarePrintBatchIn(BaseModel):
    order_ids: list[int] = Field(max_length=200)
    target: str = Field(default='', max_length=200)
    mode: str = Field(default='', max_length=20)


# ---------------------------------------------------------------------------
# Database connections are POOLED. Opening a brand-new SSL connection to Supabase for every
# request (and once per order when listing orders) was the main reason the app felt slow.
# conn() still returns an object with execute/commit/rollback/close, so every route below is unchanged:
# close() simply hands the connection back to the pool instead of dropping it.
# ---------------------------------------------------------------------------
_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            if not DB_URL:
                raise RuntimeError('DATABASE_URL is not set. Create a .env file locally or set the Render environment variable.')
            kwargs = {
                'row_factory': dict_row,
                'connect_timeout': 10,
                'prepare_threshold': None,      # safe with Supabase poolers (no server-side prepared statements)
                'keepalives': 1, 'keepalives_idle': 30, 'keepalives_interval': 10, 'keepalives_count': 3,
            }
            if 'sslmode=' not in DB_URL:
                kwargs['sslmode'] = 'require'
            def _configure(c):   # new connections work in UTC, whatever the server default is
                try:
                    c.execute("SET TIME ZONE 'UTC'")
                    c.commit()
                except Exception:
                    c.rollback()
            pool = ConnectionPool(
                DB_URL, kwargs=kwargs, open=False, configure=_configure,
                min_size=int(os.getenv('DB_POOL_MIN', '2')), max_size=int(os.getenv('DB_POOL_MAX', '10')),
                timeout=20, max_idle=300, max_lifetime=1800,
                check=ConnectionPool.check_connection,   # a connection the database closed while idle is replaced, never handed out
            )
            pool.open(wait=False)
            _pool = pool
    return _pool


class PooledConn:
    """Looks like a psycopg connection to the routes; close() returns it to the pool."""

    def __init__(self, pool):
        self._pool = pool
        self._c = pool.getconn()

    def execute(self, *a, **kw):
        return self._c.execute(*a, **kw)

    def executemany(self, query, params_seq):
        with self._c.cursor() as cur:
            cur.executemany(query, params_seq)

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def close(self):
        c, self._c = self._c, None
        if c is not None:
            try:
                # Read-only routes never commit, which leaves the connection "in a transaction". Ending it HERE (instead of
                # letting the pool do it) is the same safe rollback, but without the "rolling back returned connection" warning.
                if not c.closed and c.info.transaction_status != TransactionStatus.IDLE:
                    c.rollback()
            except Exception:
                pass
            try:
                self._pool.putconn(c)
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):   # a route that raised before close() must not leak its connection
        try:
            self.close()
        except Exception:
            pass


def conn():
    return PooledConn(_get_pool())


# Columns the application reads or writes. Startup checks them READ-ONLY; the schema itself is created and changed
# only by the versioned SQL files in migrations/ (the app no longer runs CREATE / ALTER TABLE).
REQUIRED_SCHEMA = {
    'settings': ('id', 'business_name', 'address', 'phone', 'gst_number', 'food_license', 'printer_name', 'paper_size', 'auto_print',
                 'default_order_type', 'tax_percent', 'delivery_fee', 'printer_mode', 'printer_target', 'printer_baudrate'),
    'categories': ('id', 'name', 'icon', 'sort_order'),
    'menu_items': ('id', 'name', 'category', 'price', 'available'),
    'orders': ('id', 'order_number', 'customer_name', 'phone', 'address', 'table_number', 'order_type', 'subtotal', 'delivery_fee', 'tax',
               'total', 'special_instructions', 'payment_method', 'payment_status', 'status', 'created_at'),
    'order_items': ('id', 'order_id', 'menu_item_id', 'item_name', 'quantity', 'unit_price'),
    'inventory_purchases': ('id', 'item_name', 'category', 'quantity', 'unit', 'unit_price', 'total_cost', 'purchase_date', 'notes'),
}
REQUIRED_AUTH_SCHEMA = {
    'users': ('id', 'username', 'name', 'password_hash', 'role', 'active', 'must_change_password', 'created_at'),
    'sessions': ('token_hash', 'user_id', 'created_at', 'expires_at'),
}
SCHEMA_FEATURES = {'client_request_id': False}   # optional columns added by later migrations; filled in by verify_schema()


def verify_schema(c):
    """Fail early, with a useful message, if the database has not been migrated. Never changes anything."""
    schema_row = c.execute('SELECT current_schema() AS s').fetchone()
    app_schema = (schema_row or {}).get('s') or 'public'
    wanted = {(app_schema, t): cols for t, cols in REQUIRED_SCHEMA.items()}
    wanted.update({(AUTH_SCHEMA, t): cols for t, cols in REQUIRED_AUTH_SCHEMA.items()})
    have = {}
    for r in c.execute('SELECT table_schema, table_name, column_name FROM information_schema.columns WHERE table_schema = ANY(%s)', ([app_schema, AUTH_SCHEMA],)).fetchall():
        have.setdefault((r['table_schema'], r['table_name']), set()).add(r['column_name'])
    problems = []
    for (sch, table), cols in wanted.items():
        present = have.get((sch, table))
        if present is None:
            problems.append(f'table {sch}.{table} is missing')
        else:
            gone = [col for col in cols if col not in present]
            if gone:
                problems.append(f'{sch}.{table} is missing column(s): {", ".join(gone)}')
    for table in ('categories', 'menu_items'):   # saving a category / dish omits id, so id must generate itself
        row = c.execute("SELECT column_default, is_identity FROM information_schema.columns WHERE table_schema=%s AND table_name=%s AND column_name='id'", (app_schema, table)).fetchone()
        if row and row.get('is_identity') != 'YES' and (not row.get('column_default') or str(row['column_default']).strip().upper().startswith('NULL')):
            problems.append(f'{app_schema}.{table}.id has no automatic value (apply migrations/0001 repair step)')
    if problems and env_flag('SKIP_SCHEMA_CHECK', False):
        log.warning('Schema check skipped (SKIP_SCHEMA_CHECK=true). Problems ignored: %s', '; '.join(problems))
        problems = []
    if problems:
        raise RuntimeError('The database schema is not ready: ' + '; '.join(problems) + '. Apply the SQL files in migrations/ (see docs/DEPLOYMENT.md), then restart.')
    SCHEMA_FEATURES['client_request_id'] = 'client_request_id' in have.get((app_schema, 'orders'), set())
    if not SCHEMA_FEATURES['client_request_id']:
        log.warning('orders.client_request_id is missing (migration 0003 not applied): duplicate-submit protection is OFF until it is.')
    if (app_schema, 'dining_tables') not in have:
        log.warning('dining_tables is missing (migration 0005 not applied): the live table squares stay hidden until it is.')


# ---------- Ruchira Chinese menu (Marathi names) ----------
# (marathi name, half price, full price). half = None -> single price in "full".
RUCHIRA_MENU = [
    ('नॉनव्हेज सूप', [
        ('चिकन मंचाव सूप', None, 110), ('चिकन हॉट अॅण्ड सॉर सूप', None, 110), ('चिकन न्यूडल सूप', None, 110),
        ('चिकन क्लिअर सूप', None, 110), ('चिकन टोमॅटो सूप', None, 120), ('चिकन स्वीट कॉर्न सूप', None, 120),
        ('चिकन मशरूम सूप', None, 120), ('चिकन थुप्पा सूप', None, 120), ('चिकन रुचिरा स्पेशल सूप', None, 130),
        ('चिकन लेमन कोरिअंटल सूप', None, 140)]),
    ('नॉनव्हेज ड्राय ग्रेव्ही', [
        ('लॉलीपॉप फ्राय', 110, 200), ('चिकन लॉलीपॉप अंडा फ्राय', 110, 200), ('चिकन लॉलीपॉप मसाला ड्राय', 120, 220),
        ('चिकन लॉलीपॉप ग्रेव्ही', 130, 220), ('चिकन लॉलीपॉप क्रिस्पी', 130, 230), ('चिकन मंच्युरियन ड्राय', 110, 200),
        ('चिकन चिल्ली ड्राय', 110, 210), ('चिकन मंच्युरियन ग्रेव्ही', 110, 200), ('चिकन चिल्ली ग्रेव्ही', 120, 230),
        ('चिकन ६५', 100, 200), ('चिकन हाँगकाँग ड्राय + ग्रेव्ही', 110, 220), ('चिकन रुचिरा स्पेशल + ग्रेव्ही', 140, 250)]),
    ('चिकन नूडल्स', [
        ('चिकन हक्का न्यूडल्स', 110, 190), ('चिकन शेजवान न्यूडल्स', 110, 200), ('चिकन सिंगापूर न्यूडल्स', 110, 200),
        ('चिकन मशरूम शेजवान न्यूडल्स', 110, 200), ('चिकन हाँगकाँग न्यूडल्स', 110, 200), ('चिकन कॉम्बिनेशन न्यूडल्स', 110, 200),
        ('चिकन चॉपर न्यूडल्स', 110, 220), ('चिकन मंचुरीयन न्यूडल्स + ग्रेव्ही', 110, 220), ('चिकन ट्रीपल न्यूडल्स + ग्रेव्ही', 120, 230),
        ('चिकन थाऊजंड न्यूडल्स', 110, 220), ('चिकन डायनासॉर न्यूडल्स + ग्रेव्ही', 120, 230), ('चिकन रुचिरा न्यूडल्स + ग्रेव्ही', 130, 240)]),
    ('चिकन राईस', [
        ('चिकन फ्राईड राईस', 110, 200), ('चिकन शेजवान राईस', 110, 200), ('चिकन सिंगापूर राईस', 110, 200),
        ('चिकन मशरूम शेजवान राईस', 110, 210), ('चिकन हाँगकाँग राईस', 120, 210), ('चिकन कॉम्बिनेशन राईस', 120, 210),
        ('चिकन चॉपर राईस', 110, 210), ('चिकन मंचुरीयन राईस + ग्रेव्ही', 110, 210), ('चिकन ट्रीपल राईस + ग्रेव्ही', 120, 230),
        ('चिकन थाऊजंड राईस', 110, 210), ('चिकन डायनासॉर राईस + ग्रेव्ही', 130, 240), ('चिकन रुचिरा राईस + ग्रेव्ही', 130, 250)]),
    ('व्हेज सूप', [
        ('व्हेज मंचाव सूप', None, 100), ('व्हेज हॉट अॅण्ड सॉर सूप', None, 100), ('व्हेज न्यूडल सूप', None, 100),
        ('व्हेज क्लिअर सूप', None, 100), ('व्हेज टोमॅटो सूप', None, 120), ('व्हेज स्वीट कॉर्न सूप', None, 110),
        ('व्हेज मशरूम सूप', None, 110), ('व्हेज थुप्पा सूप', None, 110), ('व्हेज रुचिरा स्पेशल सूप', None, 130)]),
    ('व्हेज स्टार्टर', [
        ('पनीर क्रिस्पी', 100, 190), ('पनीर चिल्ली', 100, 190), ('पनीर मंचुरियन', 100, 190), ('मशरूम चिल्ली', 110, 200),
        ('व्हेज क्रिस्पी', 100, 180), ('व्हेज मंचुरियन ड्राय', 100, 180), ('पनीर चिल्ली ग्रेव्ही', 110, 200),
        ('व्हेज हाँगकाँग ड्राय', 110, 200), ('व्हेज शेजवान ड्राय', 110, 200), ('व्हेज गार्लिक ड्राय', 110, 200),
        ('व्हेज जिंजर ड्राय', 110, 200)]),
    ('व्हेज भेळ', [('व्हेज चायनीज भेळ', None, 140), ('व्हेज शेजवान भेळ', None, 150)]),
    ('व्हेज चॉपसी', [('व्हेज अमेरिकन चॉपसी', None, 140), ('व्हेज शेजवान चॉपसी', None, 150)]),
    ('व्हेज न्यूडल्स', [
        ('व्हेज हक्का न्यूडल्स', 100, 180), ('व्हेज शेजवान न्यूडल्स', 110, 190), ('व्हेज सिंगापूर न्यूडल्स', 110, 190),
        ('पनीर शेजवान न्यूडल्स', 110, 190), ('व्हेज मशरूम शेजवान न्यूडल्स', 110, 190), ('व्हेज हाँगकाँग न्यूडल्स', 110, 190),
        ('व्हेज कॉम्बिनेशन न्यूडल्स', 110, 190), ('व्हेज चॉपर न्यूडल्स', 110, 200), ('व्हेज मंचुरीयन न्यूडल्स + ग्रेव्ही', 110, 200),
        ('व्हेज ट्रीपल न्यूडल्स + ग्रेव्ही', 110, 190), ('व्हेज थाऊंड न्यूडल्स + ग्रेव्ही', 110, 200),
        ('पनीर शेजवान न्यूडल्स (स्पेशल)', 110, 210), ('व्हेज डायनासॉर न्यूडल्स + ग्रेव्ही', 120, 210),
        ('व्हेज रुचिरा न्यूडल्स + ग्रेव्ही', 130, 240)]),
    ('व्हेज राईस', [
        ('व्हेज फ्राईड राईस', 100, 180), ('व्हेज शेजवान राईस', 110, 190), ('व्हेज सिंगापूर राईस', 110, 190),
        ('पनीर शेजवान राईस', 110, 190), ('व्हेज मशरूम शेजवान राईस', 110, 190), ('व्हेज हाँगकाँग राईस', 110, 190),
        ('व्हेज कॉम्बिनेशन राईस', 110, 190), ('व्हेज चॉपर राईस', 110, 200), ('व्हेज मंचुरीयन राईस + ग्रेव्ही', 110, 200),
        ('व्हेज ट्रीपल राईस + ग्रेव्ही', 110, 200), ('व्हेज थाऊजंड राईस + ग्रेव्ही', 110, 200),
        ('व्हेज डायनासॉर राईस + ग्रेव्ही', 120, 220), ('व्हेज रुचिरा राईस + ग्रेव्ही', 130, 230)]),
]


def seed_ruchira_menu(c):
    """Adds the Ruchira Chinese veg / non-veg menu (Marathi names) once.
    Runs only while the first category (नॉनव्हेज सूप) does not exist yet, so items the owner later
    renames, re-prices or deletes are never put back on restart. Half / Full are separate items."""
    if not env_flag('SEED_RUCHIRA_MENU', False):
        return   # off by default: another restaurant's instance must never receive this menu
    if c.execute('SELECT 1 FROM categories WHERE name=%s', (RUCHIRA_MENU[0][0],)).fetchone():
        return
    max_order = c.execute('SELECT COALESCE(MAX(sort_order), -1) AS n FROM categories').fetchone()['n']
    for cat, items in RUCHIRA_MENU:
        max_order += 1
        c.execute('INSERT INTO categories(name,icon,sort_order) VALUES (%s,%s,%s)', (cat, '', max_order))
        for name, half, full in items:
            rows = [(name, full)] if half is None else [(name + ' (हाफ)', half), (name + ' (फुल)', full)]
            for n, price in rows:
                if not c.execute('SELECT 1 FROM menu_items WHERE name=%s AND category=%s', (n, cat)).fetchone():
                    c.execute('INSERT INTO menu_items(name,category,price,available) VALUES (%s,%s,%s,%s)', (n, cat, price, True))


def init_db():
    """Startup: READ-ONLY schema check, first-owner bootstrap, default-password flagging, optional seed data.
    It never runs CREATE / ALTER TABLE (see migrations/)."""
    c = conn()
    try:
        verify_schema(c)
        bootstrap_owner(c)
        flag_default_password_accounts(c)
        if not c.execute('SELECT 1 FROM settings WHERE id=1').fetchone():
            c.execute(
                'INSERT INTO settings (id,business_name,address,phone,gst_number,printer_name,paper_size,auto_print,default_order_type,tax_percent,delivery_fee,printer_mode,printer_target,printer_baudrate) VALUES (1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (os.getenv('BUSINESS_NAME', 'My Kitchen')[:120], '', '', '', '58mm Mini Thermal Printer', '58mm', False, 'Pickup', 0, 0, 'browser', '', 9600)
            )
        # Starter categories only for a brand-new, empty menu (they used to be re-added on EVERY restart, even after the owner deleted them).
        if c.execute('SELECT COUNT(*) AS n FROM categories').fetchone()['n'] == 0 and c.execute('SELECT COUNT(*) AS n FROM menu_items').fetchone()['n'] == 0:
            for idx, (name, icon) in enumerate([('Pizza', '🍕'), ('Burgers', '🍔'), ('Paneer', ''), ('Sandwiches', '🥪'), ('Snacks', '🍟'), ('Beverages', '🥤')]):
                c.execute('INSERT INTO categories(name,icon,sort_order) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING', (name, icon, idx))

        seed_ruchira_menu(c)

        if DEMO_ENABLED:
            if c.execute('SELECT COUNT(*) AS n FROM menu_items').fetchone()['n'] == 0:
                items = [
                    ('Margherita Pizza', 'Pizza', 199, True), ('Farmhouse Pizza', 'Pizza', 249, True),
                    ('Paneer Tikka Pizza', 'Pizza', 279, True), ('Veggie Supreme Pizza', 'Pizza', 299, True),
                    ('Veg Burger', 'Burgers', 129, True), ('Cheese Burst Burger', 'Burgers', 169, True),
                    ('Paneer Burger', 'Burgers', 159, True), ('Classic Burger', 'Burgers', 149, True),
                    ('French Fries', 'Snacks', 99, True), ('Masala Fries', 'Snacks', 119, True),
                    ('Paneer Wrap', 'Sandwiches', 149, True), ('Grilled Veg Sandwich', 'Sandwiches', 139, True),
                    ('Paneer Lababdar', 'Paneer', 229, True), ('Paneer Masala', 'Paneer', 219, True),
                    ('Cold Coffee', 'Beverages', 99, True), ('Fresh Lime Soda', 'Beverages', 79, True)
                ]
                c.executemany('INSERT INTO menu_items(name,category,price,available) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING', items)
            # Demo ORDERS are only seeded when SEED_DEMO_ORDERS=true, so clearing orders stays cleared after a restart.
            if env_flag('SEED_DEMO_ORDERS', False) and c.execute('SELECT COUNT(*) AS n FROM orders').fetchone()['n'] == 0:
                seed_orders(c)
            if c.execute('SELECT COUNT(*) AS n FROM inventory_purchases').fetchone()['n'] == 0:
                seed_inventory(c)
        c.commit()
    finally:
        c.close()

def seed_orders(c):
    menu = {r['name']: r for r in c.execute('SELECT * FROM menu_items').fetchall()}
    seed = [
        ('Rahul Sharma', '9876543210', 'Baner, Pune', 'Pickup', [('Veg Burger', 2), ('French Fries', 1)], 'Less spicy', 'UPI', 'Paid', 'New', 10),
        ('Neha Patil', '9822001122', 'Aundh, Pune', 'Delivery', [('Margherita Pizza', 1), ('Cold Coffee', 2)], 'Call on arrival', 'UPI', 'Paid', 'Accepted', 25),
        ('Amit Joshi', '9890012345', 'Wakad, Pune', 'Delivery', [('Paneer Wrap', 2), ('Masala Fries', 1)], 'No onion', 'Cash', 'Pending', 'Preparing', 30),
        ('Priya Desai', '9765012345', 'Kothrud, Pune', 'Pickup', [('Farmhouse Pizza', 1)], 'Extra cheese', 'Card', 'Paid', 'Ready', 0),
        ('Vikram Kulkarni', '9712345678', 'Hinjewadi, Pune', 'Delivery', [('Cheese Burst Burger', 2), ('Cold Coffee', 1)], 'Pack separately', 'UPI', 'Paid', 'Completed', 30),
        ('Sneha More', '9811112233', 'Viman Nagar, Pune', 'Pickup', [('Margherita Pizza', 2)], '', 'UPI', 'Paid', 'Completed', 0),
        ('Arjun Shah', '9900011223', 'Kharadi, Pune', 'Delivery', [('Veg Burger', 1), ('French Fries', 2)], 'Extra ketchup', 'Cash', 'Paid', 'Completed', 20),
        ('Pooja Kale', '9822334455', 'Pimple Saudagar, Pune', 'Pickup', [('Paneer Wrap', 1), ('Cold Coffee', 1)], '', 'UPI', 'Paid', 'Cancelled', 0),
        ('Rohan Gawde', '9898989898', 'Bavdhan, Pune', 'Delivery', [('Farmhouse Pizza', 1), ('French Fries', 1)], 'Well done', 'UPI', 'Paid', 'Preparing', 25),
        ('Meera Rao', '9988776655', 'Magarpatta, Pune', 'Pickup', [('Veg Burger', 1), ('Masala Fries', 1)], '', 'Card', 'Paid', 'Completed', 0),
    ]
    start = core.utc_now() - timedelta(hours=8)
    for idx, rec in enumerate(seed, start=1):
        customer, phone, addr, ot, items, note, pm, ps, status, delivery = rec
        subtotal = sum(menu[n]['price'] * q for n, q in items)
        tax = 0
        total = subtotal + delivery
        created = core.db_timestamp((start + timedelta(minutes=idx * 37)).replace(microsecond=0))
        oid = c.execute(
            'INSERT INTO orders(order_number,customer_name,phone,address,order_type,subtotal,delivery_fee,tax,total,special_instructions,payment_method,payment_status,status,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (1000 + idx, customer, phone, addr, ot, subtotal, delivery, tax, total, note, pm, ps, status, created)
        ).fetchone()['id']
        for n, q in items:
            mi = menu[n]
            c.execute('INSERT INTO order_items(order_id,menu_item_id,item_name,quantity,unit_price) VALUES (%s,%s,%s,%s,%s)', (oid, mi['id'], n, q, mi['price']))


def seed_inventory(c):
    today_d = core.ist_today()
    today = today_d.isoformat()
    month_offset = (today_d.replace(day=1) - timedelta(days=10)).replace(day=10).isoformat()
    data = [
        ('Tomatoes', 'Vegetables', 5, 'kg', 100, today, 'Demo purchase'),
        ('Onion', 'Vegetables', 5, 'kg', 55, today, ''),
        ('Paneer', 'Dairy', 3, 'kg', 280, month_offset, ''),
        ('Burger Buns', 'Bakery', 50, 'pcs', 8, month_offset, ''),
        ('Cheese', 'Dairy', 2, 'kg', 420, month_offset, ''),
    ]
    c.executemany(
        'INSERT INTO inventory_purchases(item_name,category,quantity,unit,unit_price,total_cost,purchase_date,notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
        [(n, cat, q, u, p, q * p, d, note) for n, cat, q, u, p, d, note in data]
    )


def serialize_orders(c, rows):
    """Attach the items to many orders with ONE query (it used to open a new connection per order)."""
    rows = list(rows)
    if not rows:
        return []
    by_order = {}
    for r in c.execute('SELECT order_id,menu_item_id,item_name,quantity,unit_price FROM order_items WHERE order_id = ANY(%s) ORDER BY order_id,id', ([row['id'] for row in rows],)).fetchall():
        by_order.setdefault(r['order_id'], []).append({'menu_item_id': r['menu_item_id'], 'item_name': r['item_name'], 'quantity': r['quantity'], 'unit_price': r['unit_price']})
    out = []
    for row in rows:
        d = dict(row)
        d['items'] = by_order.get(row['id'], [])
        out.append(d)
    return out


def serialize_order(row):
    c = conn()
    try:
        return serialize_orders(c, [row])[0]
    finally:
        c.close()


def business_name(settings):
    return (settings.get('business_name') or '').strip() or 'Demo Kitchen'


def _india_order_datetime(value):
    """Stored naive timestamps are UTC; show them in India time."""
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or '').strip().replace('Z', '+00:00')
        dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=core.UTC)   # stored timestamps are naive UTC clock values
    return dt.astimezone(core.IST)


def print_ticket_text(order, settings):
    width = 32
    created_dt = _india_order_datetime(order.get('created_at'))

    def line(char='-'):
        return char * width

    def wrapped(label, value):
        s = f'{label}: {value}'
        return '\n'.join(textwrap.wrap(s, width=width))

    name = business_name(settings)
    lines = [t.center(width).rstrip() for t in (textwrap.wrap(name, width=width) or [name])]
    if (settings.get('address') or '').strip():
        lines += [t.center(width).rstrip() for t in textwrap.wrap('Address: ' + settings['address'].strip(), width=width)]
    if (settings.get('phone') or '').strip():
        lines += [t.center(width).rstrip() for t in textwrap.wrap('Phone: ' + settings['phone'].strip(), width=width)]
    if (settings.get('gst_number') or '').strip():
        lines += [t.center(width).rstrip() for t in textwrap.wrap('GSTIN: ' + settings['gst_number'].strip(), width=width)]
    if (settings.get('food_license') or '').strip():
        lines += [t.center(width).rstrip() for t in textwrap.wrap('FSSAI Lic: ' + settings['food_license'].strip(), width=width)]
    lines += [
        ' ' * 10 + 'KITCHEN ORDER',
        line(),
        f"Order: #{order['order_number']}",
        f"Date: {created_dt.strftime('%d-%m-%Y')}",
        f"Time: {created_dt.strftime('%I:%M %p')}",
        line(),
    ]
    # name, phone and address are optional: only print what was entered
    who = []
    if (order.get('table_number') or '').strip(): who.append(wrapped('Table', order['table_number'].strip()))
    if (order.get('customer_name') or '').strip(): who.append(wrapped('Customer', order['customer_name'].strip()))
    if (order.get('phone') or '').strip(): who.append(wrapped('Phone', order['phone'].strip()))
    if (order.get('address') or '').strip(): who.append(wrapped('Address', order['address'].strip()))
    if who:
        lines += who + [line()]
    for item in order['items']:
        item_name = item['item_name'][:20]
        price = f"Rs {item['unit_price'] * item['quantity']:.0f}"
        lines.append(f"{item['quantity']}x {item_name:<20} {price:>7}")
    lines += [
        line(),
        f"{'Subtotal':<23} Rs {order['subtotal']:.0f}",
        f"{'Delivery':<23} Rs {order['delivery_fee']:.0f}",
        f"{'Tax':<23} Rs {order['tax']:.0f}",
        f"{'TOTAL':<23} Rs {order['total']:.0f}",
        line(),
        f"Type: {order['order_type']}",
        f"Payment: {'UPI' if str(order.get('payment_method') or '').strip().upper() == 'UPI' else 'Cash'}",
    ]
    if order.get('special_instructions'):
        lines += [line(), wrapped('Note', order['special_instructions'])]
    lines += [line(), ' ' * 11 + 'THANK YOU', ' ' * 10 + 'VISIT AGAIN', '', '']
    return '\n'.join(lines)


def _receipt_font(size):
    """Find a system font with Devanagari glyphs for direct ESC/POS bitmap printing."""
    from PIL import ImageFont
    candidates = [
        Path(os.getenv('WINDIR', r'C:\Windows')) / 'Fonts' / 'Nirmala.ttf',
        Path(os.getenv('WINDIR', r'C:\Windows')) / 'Fonts' / 'NirmalaS.ttf',
        Path(os.getenv('WINDIR', r'C:\Windows')) / 'Fonts' / 'mangal.ttf',
        Path('/usr/share/fonts/truetype/freefont/FreeSans.ttf'),
        Path('/usr/share/fonts/opentype/freefont/FreeSans.otf'),
        Path('/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf'),
        Path('/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf'),
        Path('/usr/share/fonts/truetype/noto/NotoSansDevanagariUI-Regular.ttf'),
        Path('/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf'),
    ]
    engine = getattr(ImageFont, 'Layout', None)
    layout = getattr(engine, 'RAQM', None) if engine else None
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size, layout_engine=layout) if layout is not None else ImageFont.truetype(str(path), size)
            except Exception:
                continue
    raise RuntimeError('Marathi thermal printing needs a Devanagari font (Nirmala UI/Mangal on Windows or Noto Sans Devanagari on Linux).')


def _wrap_to_pixels(draw, text, font, max_width):
    result = []
    for paragraph in str(text or '').split('\n'):
        current = ''
        for word in paragraph.split():
            candidate = f'{current} {word}'.strip()
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
                continue
            if current:
                result.append(current)
                current = ''
            if draw.textlength(word, font=font) <= max_width:
                current = word
                continue
            # Very long unbroken dish names are split at character boundaries rather than clipped.
            chunk = ''
            for char in word:
                if chunk and draw.textlength(chunk + char, font=font) > max_width:
                    result.append(chunk)
                    chunk = char
                else:
                    chunk += char
            current = chunk
        if current:
            result.append(current)
        elif not paragraph:
            result.append('')
    return result or ['']



def _escpos_bitmap(image):
    """Convert a Pillow image to standard ESC/POS GS v 0 raster data (58mm, 384 dots)."""
    from PIL import Image
    image = image.convert('L')
    width, height = image.size
    pixels = image.load()
    bytes_per_row = (width + 7) // 8
    payload = bytearray((0x1D, 0x76, 0x30, 0x00, bytes_per_row & 0xFF, (bytes_per_row >> 8) & 0xFF, height & 0xFF, (height >> 8) & 0xFF))
    for y in range(height):
        for byte_x in range(bytes_per_row):
            value = 0
            for bit in range(8):
                x = byte_x * 8 + bit
                if x < width and pixels[x, y] < 180:
                    value |= 0x80 >> bit
            payload.append(value)
    return bytes(payload)


def _raster_text_line(text, align='left', right='', font_size=24):
    from PIL import Image, ImageDraw
    width, margin = 384, 5
    font = _receipt_font(font_size)
    measure_image = Image.new('L', (width, 40), 255)
    measure = ImageDraw.Draw(measure_image)
    right_width = measure.textlength(right, font=font) if right else 0
    max_left = width - margin * 2 - (right_width + 18 if right else 0)
    lines = _wrap_to_pixels(measure, text, font, max(40, max_left))
    line_height = font_size + 9
    height = max(line_height, len(lines) * line_height + 2)
    image = Image.new('L', (width, height), 255)
    draw = ImageDraw.Draw(image)
    for idx, item in enumerate(lines):
        length = draw.textlength(item, font=font)
        if align == 'center': x = max(margin, (width - length) / 2)
        elif align == 'right': x = max(margin, width - margin - length)
        else: x = margin
        y = idx * line_height + 2
        draw.text((x, y), item, font=font, fill=0)
        if idx == 0 and right:
            draw.text((width - margin - right_width, y), right, font=font, fill=0)
    return _escpos_bitmap(image)


def _raster_item_row(label, price):
    return _raster_text_line(label, align='left', right=price, font_size=30)


def escpos_order_bytes(order, settings):
    """Use bitmap text for Unicode/item rows so printers without Devanagari code pages print Marathi correctly."""
    text = print_ticket_text(order, settings)

    # Match formatted item lines to their source records; item rows are drawn larger with separate left/right columns.
    item_rows = {}
    for item in order.get('items', []):
        item_name = str(item.get('item_name') or '')[:20]
        price = f"Rs {item['unit_price'] * item['quantity']:.0f}"
        key = f"{item['quantity']}x {item_name:<20} {price:>7}"
        item_rows.setdefault(key, []).append((f"{item['quantity']}x {item.get('item_name') or ''}", price))

    lines = text.splitlines()
    order_line = next((idx for idx, line in enumerate(lines) if line.startswith('Order: #')), len(lines))
    output = bytearray(b'\x1b@\x1bG\x01\x1bE\x01\x1b!\x18')
    for idx, line in enumerate(lines):
        rows = item_rows.get(line)
        if rows:
            label, price = rows.pop(0)
            try:
                output.extend(_raster_item_row(label, price))
            except Exception:
                # Preserve ordinary English printing if this system lacks a usable raster font.
                if all(ord(char) < 128 for char in label + price):
                    output.extend(line.encode('ascii', errors='replace') + b'\n')
                else:
                    raise
        elif any(ord(char) > 127 for char in line):
            output.extend(_raster_text_line(line.strip(), align='center' if idx < order_line else 'left'))
        else:
            output.extend(line.encode('ascii', errors='replace') + b'\n')
    output.extend(b'\n\n\x1dV\x41\x03')
    return bytes(output)


def escpos_bytes(text: str) -> bytes:
    # UTF-8 is fine for ordinary ASCII receipts; Unicode rows use escpos_order_bytes() above.
    return b'\x1b@\x1bG\x01\x1bE\x01\x1b!\x18' + text.encode('utf-8', errors='replace') + b'\n\n\x1dV\x41\x03'


@app.on_event('startup')
def startup():
    init_db()


# ------------------------------------------------------------------ login, sessions and users
# AUTH_REQUIRED=false is an emergency/development switch only. In production it is IGNORED, because it makes every visitor an owner.
_auth_flag = env_flag('AUTH_REQUIRED', True)
if not _auth_flag and PRODUCTION:
    log.error('AUTH_REQUIRED=false is ignored because APP_ENV is production. Remove it from the environment.')
AUTH_REQUIRED = True if PRODUCTION else _auth_flag
COOKIE = 'dk_session'
SESSION_DAYS = 30
MIN_PASSWORD = core.MIN_PASSWORD
USERNAME_RE = re.compile(r'^[a-z0-9._-]{3,32}$')
GUEST = {'id': 0, 'username': 'guest', 'name': 'Guest', 'role': 'owner', 'active': True, 'must_change_password': False, 'auth_disabled': True}
_session_cache = {}   # token_hash -> (valid_until, user)   (per process; users who must change their password are never cached)
login_throttle = core.LoginThrottle(pair_limit=6, ip_limit=30, window=600)
password_throttle = core.LoginThrottle(pair_limit=8, ip_limit=40, window=600)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode('ascii')


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    try:
        dk = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1, dklen=32)
        return f'scrypt${_b64(salt)}${_b64(dk)}'
    except (AttributeError, ValueError):   # Python built without scrypt
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 240000, 32)
        return f'pbkdf2${_b64(salt)}${_b64(dk)}'


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, hash_b64 = stored.split('$')
        salt, want = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        if scheme == 'scrypt':
            got = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1, dklen=32)
        elif scheme == 'pbkdf2':
            got = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 240000, 32)
        else:
            return False
        return hmac.compare_digest(got, want)
    except Exception:
        return False


DUMMY_HASH = hash_password('not-a-real-password')   # so unknown usernames cost the same time as wrong passwords


def bootstrap_owner(c):
    """Create the FIRST owner, only on a database with no users, and only from an explicitly configured password.
    There is no built-in default account. Existing users are never touched, reset or recreated."""
    if c.execute(f'SELECT COUNT(*) AS n FROM {T_USERS}').fetchone()['n'] > 0:
        return
    username = (os.getenv('ADMIN_USERNAME') or 'admin').strip().lower()
    password = os.getenv('ADMIN_PASSWORD') or ''
    if not USERNAME_RE.match(username):
        raise RuntimeError('ADMIN_USERNAME must be 3 to 32 characters: lower-case letters, numbers, dot, dash or underscore.')
    if not password:
        raise RuntimeError('This database has no users yet, so the first owner must be created, but ADMIN_PASSWORD is not set. '
                           'Set ADMIN_PASSWORD (and optionally ADMIN_USERNAME) in the environment for the first start, then remove it. '
                           'See docs/DEPLOYMENT.md.')
    problem = core.password_problem(password, username)
    if problem:
        raise RuntimeError('ADMIN_PASSWORD is not acceptable: ' + problem)
    try:
        c.execute(
            f'INSERT INTO {T_USERS}(username,name,password_hash,role,active,must_change_password,created_at) VALUES (%s,%s,%s,%s,TRUE,FALSE,%s)',
            (username, 'Owner', hash_password(password), 'owner', core.utc_now().isoformat(timespec='seconds'))
        )
        c.commit()
        log.info('Created the first owner account "%s".', username)
    except errors.UniqueViolation:   # another instance won the race
        c.rollback()


def flag_default_password_accounts(c):
    """An existing database may still contain a login that accepts a well-known default password (for example one left over from an early version).
    Do NOT delete or overwrite it: just require a password change before that login can use anything else."""
    rows = c.execute(f'SELECT id, username, password_hash FROM {T_USERS} WHERE NOT must_change_password').fetchall()
    flagged = [r for r in rows if any(verify_password(d, r['password_hash']) for d in core.KNOWN_DEFAULT_PASSWORDS)]
    for r in flagged:
        c.execute(f'UPDATE {T_USERS} SET must_change_password=TRUE WHERE id=%s', (r['id'],))
    if flagged:
        c.commit()
        log.warning('These logins still use a well-known default password and must change it at next sign-in: %s',
                    ', '.join(repr(r['username']) for r in flagged))


def public_user(row):
    return {'id': row['id'], 'username': row['username'], 'name': row['name'] or row['username'], 'role': row['role'],
            'active': bool(row['active']), 'must_change_password': bool(row['must_change_password'])}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def create_session(c, user):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    c.execute(f'DELETE FROM {T_SESSIONS} WHERE expires_at < %s', (now,))
    c.execute(f'INSERT INTO {T_SESSIONS}(token_hash,user_id,created_at,expires_at) VALUES (%s,%s,%s,%s)', (_token_hash(token), user['id'], now, now + SESSION_DAYS * 86400))
    c.commit()
    if not user.get('must_change_password'):
        _session_cache[_token_hash(token)] = (time.time() + 60, user)
    return token


def lookup_session(token: str):
    th = _token_hash(token)
    now = time.time()
    hit = _session_cache.get(th)
    if hit and hit[0] > now:
        return hit[1]
    c = conn()
    try:
        row = c.execute(f'SELECT u.* FROM {T_SESSIONS} s JOIN {T_USERS} u ON u.id = s.user_id WHERE s.token_hash=%s AND s.expires_at>%s AND u.active', (th, int(now))).fetchone()
    finally:
        c.close()
    if not row:
        _session_cache.pop(th, None)
        return None
    user = public_user(row)
    if user['must_change_password']:
        _session_cache.pop(th, None)   # always re-read from the database, so the flag clears the moment the password is changed
    else:
        _session_cache[th] = (now + 60, user)
    return user


def forget_sessions(user_id: int):
    for key, (_, user) in list(_session_cache.items()):
        if user['id'] == user_id:
            _session_cache.pop(key, None)


def current_user(request: Request):
    return GUEST if not AUTH_REQUIRED else getattr(request.state, 'user', None)


def require_owner(request: Request):
    """Dependency for every owner-only route (defence in depth: the middleware enforces the same table in backend/core.py)."""
    user = current_user(request)
    if not user:
        raise HTTPException(401, 'Please log in.')
    if user.get('role') != 'owner':
        raise HTTPException(403, 'Only the owner can do this.')
    return user


OWNER = [Depends(require_owner)]


def _is_https(request: Request) -> bool:
    return request.url.scheme == 'https' or request.headers.get('x-forwarded-proto', '').split(',')[0].strip() == 'https'


def _client_ip(request: Request) -> str:
    return core.client_ip(request.headers.get('x-forwarded-for', ''), request.client.host if request.client else '', TRUSTED_PROXY_HOPS)


def _harden(resp, request: Request, path: str):
    for key, value in core.security_headers(https=_is_https(request) or PRODUCTION, csp_mode=CSP_MODE).items():
        resp.headers[key] = value
    if path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'   # never keep orders / logins in a shared device's cache
    return resp


def _json(request: Request, path: str, status: int, detail: str, **extra):
    return _harden(JSONResponse({'detail': detail, **extra}, status_code=status), request, path)


@app.middleware('http')
async def auth_guard(request: Request, call_next):
    path = core.normalise_path(request.url.path)
    docs = path in ('/docs', '/redoc', '/openapi.json', '/docs/oauth2-redirect')
    declared = request.headers.get('content-length', '')
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return _json(request, path, 413, 'That request is too large.')
    if not (path.startswith('/api/') or docs) or request.method == 'OPTIONS':
        return _harden(await call_next(request), request, path)
    needed = 'owner' if docs else core.required_role(request.method, path)
    if needed == 'public' or not AUTH_REQUIRED:
        return _harden(await call_next(request), request, path)
    token = request.cookies.get(COOKIE)
    user = None
    if token:
        try:
            user = await run_in_threadpool(lookup_session, token)
        except Exception:
            log.exception('Session lookup failed')   # details stay in the server log, never in the response
            return _json(request, path, 503, 'The login service is not available right now. Please try again in a moment.')
    if not user:
        return _json(request, path, 401, 'Please log in.')
    if user['must_change_password'] and path not in core.PASSWORD_CHANGE_PATHS:
        # The password change is enforced HERE, on the server, for every other API call.
        return _json(request, path, 403, 'You must choose a new password before you can use the app.', code='password_change_required')
    if needed == 'owner' and user['role'] != 'owner':
        return _json(request, path, 403, 'Only the owner can do this.')
    request.state.user = user
    return _harden(await call_next(request), request, path)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # A short readable message. FastAPI's default echoes the submitted values back, which we never want for passwords.
    parts = []
    for e in exc.errors()[:3]:
        where = '.'.join(str(x) for x in e.get('loc', ()) if x not in ('body', 'query', 'path'))
        msg = str(e.get('msg', 'invalid')).replace('Value error, ', '')
        parts.append(f'{where}: {msg}' if where else msg)
    return JSONResponse({'detail': 'Please check the details you entered. ' + '; '.join(parts)}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    log.exception('Unhandled error on %s %s', request.method, request.url.path)   # full detail in the server log only
    return JSONResponse({'detail': 'Something went wrong on the server. Please try again.'}, status_code=500)


@app.post('/api/login')
def login(payload: LoginIn, request: Request):
    username = payload.username.strip().lower()
    ip = _client_ip(request)
    if login_throttle.blocked(ip, username):
        raise HTTPException(429, 'Too many wrong attempts from this device. Please wait 10 minutes and try again.')
    c = conn()
    try:
        row = c.execute(f'SELECT * FROM {T_USERS} WHERE username=%s', (username,)).fetchone()
        ok = verify_password(payload.password, row['password_hash'] if row else DUMMY_HASH) and bool(row) and bool(row['active'])
        if not ok:
            login_throttle.record_failure(ip, username)
            log.warning('Failed login for %r from %s', username[:32], ip)
            if login_throttle.username_failures(username) in (25, 100, 400):   # visible alert; it never locks anybody out
                log.error('Possible password guessing against %r: %s recent failures from many sources', username[:32], login_throttle.username_failures(username))
            raise HTTPException(401, 'Wrong username or password.')
        login_throttle.record_success(ip, username)
        user = public_user(row)
        token = create_session(c, user)
    finally:
        c.close()
    resp = JSONResponse(user)
    resp.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite='lax', secure=(PRODUCTION or _is_https(request)), path='/')
    return resp


@app.post('/api/logout')
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        th = _token_hash(token)
        _session_cache.pop(th, None)
        try:
            c = conn()
            try:
                c.execute(f'DELETE FROM {T_SESSIONS} WHERE token_hash=%s', (th,))
                c.commit()
            finally:
                c.close()
        except Exception:
            log.exception('Could not delete the session on logout')
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(COOKIE, path='/')
    return resp


@app.get('/api/me')
def whoami(request: Request):
    return current_user(request)


@app.post('/api/me/password')
def change_my_password(payload: PasswordChangeIn, request: Request):
    user = current_user(request)
    if user.get('auth_disabled'):
        raise HTTPException(400, 'Login is switched off on this server.')
    ip = _client_ip(request)
    if password_throttle.blocked(ip, user['username']):
        raise HTTPException(429, 'Too many wrong attempts. Please wait 10 minutes and try again.')
    problem = core.password_problem(payload.new_password, user['username'])
    if problem:
        raise HTTPException(400, problem)
    c = conn()
    try:
        row = c.execute(f'SELECT * FROM {T_USERS} WHERE id=%s', (user['id'],)).fetchone()
        if not row or not verify_password(payload.current_password, row['password_hash']):
            password_throttle.record_failure(ip, user['username'])
            raise HTTPException(400, 'Your current password is not correct.')
        if payload.new_password == payload.current_password:
            raise HTTPException(400, 'Please choose a different password from the current one.')
        # One transaction: the new hash is saved AND the forced-change flag cleared only if the commit succeeds.
        c.execute(f'UPDATE {T_USERS} SET password_hash=%s, must_change_password=FALSE WHERE id=%s', (hash_password(payload.new_password), user['id']))
        c.execute(f'DELETE FROM {T_SESSIONS} WHERE user_id=%s AND token_hash<>%s', (user['id'], _token_hash(request.cookies.get(COOKIE, ''))))   # other devices must log in again
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    password_throttle.record_success(ip, user['username'])
    forget_sessions(user['id'])
    audit_log.info('password_changed user_id=%s', user['id'])
    return {'ok': True}


def _owner_count(c) -> int:
    return c.execute(f"SELECT COUNT(*) AS n FROM {T_USERS} WHERE role='owner' AND active").fetchone()['n']


@app.get('/api/users', dependencies=OWNER)
def list_users():
    c = conn()
    try:
        return [public_user(r) for r in c.execute(f'SELECT * FROM {T_USERS} ORDER BY id').fetchall()]
    finally:
        c.close()


@app.post('/api/users', dependencies=OWNER)
def create_user(payload: UserCreateIn, request: Request):
    username = payload.username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, 'Username must be 3 to 32 characters: letters, numbers, dot, dash or underscore.')
    problem = core.password_problem(payload.password, username)
    if problem:
        raise HTTPException(400, problem)
    c = conn()
    try:
        if c.execute(f'SELECT 1 FROM {T_USERS} WHERE username=%s', (username,)).fetchone():
            raise HTTPException(409, 'That username is already taken.')
        # The owner chooses a temporary password; the person must replace it the first time they sign in.
        row = c.execute(
            f'INSERT INTO {T_USERS}(username,name,password_hash,role,active,must_change_password,created_at) VALUES (%s,%s,%s,%s,TRUE,TRUE,%s) RETURNING *',
            (username, payload.name.strip(), hash_password(payload.password), payload.role, core.utc_now().isoformat(timespec='seconds'))
        ).fetchone()
        c.commit()
        audit_log.info('user_created id=%s role=%s by=%s', row['id'], payload.role, current_user(request)['id'])
        return public_user(row)
    except errors.UniqueViolation:
        c.rollback()
        raise HTTPException(409, 'That username is already taken.')
    finally:
        c.close()


@app.put('/api/users/{user_id}', dependencies=OWNER)
def update_user(user_id: int, payload: UserUpdateIn, request: Request):
    me = current_user(request)
    c = conn()
    try:
        row = c.execute(f'SELECT * FROM {T_USERS} WHERE id=%s', (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'User not found')
        name = row['name'] if payload.name is None else payload.name.strip()
        role = row['role'] if payload.role is None else payload.role
        active = bool(row['active']) if payload.active is None else payload.active
        if row['role'] == 'owner' and row['active'] and (role != 'owner' or not active) and _owner_count(c) <= 1:
            raise HTTPException(400, 'There must always be at least one active owner.')
        if user_id == me['id'] and not active:
            raise HTTPException(400, "You can't switch off your own login.")
        new_password = (payload.password or '')
        if new_password:
            problem = core.password_problem(new_password, row['username'])
            if problem:
                raise HTTPException(400, problem)
        c.execute(f'UPDATE {T_USERS} SET name=%s, role=%s, active=%s WHERE id=%s', (name, role, active, user_id))
        if new_password:
            # Resetting someone else's password gives them a temporary one: they must choose their own at next sign-in.
            c.execute(f'UPDATE {T_USERS} SET password_hash=%s, must_change_password=%s WHERE id=%s', (hash_password(new_password), user_id != me['id'], user_id))
        if new_password or not active or role != row['role']:
            keep = _token_hash(request.cookies.get(COOKIE, '')) if user_id == me['id'] else ''
            c.execute(f'DELETE FROM {T_SESSIONS} WHERE user_id=%s AND token_hash<>%s', (user_id, keep))
        c.commit()
        updated = c.execute(f'SELECT * FROM {T_USERS} WHERE id=%s', (user_id,)).fetchone()
    finally:
        c.close()
    forget_sessions(user_id)
    audit_log.info('user_updated id=%s role=%s active=%s password_reset=%s by=%s', user_id, role, active, bool(new_password), me['id'])
    return public_user(updated)


@app.delete('/api/users/{user_id}', dependencies=OWNER)
def delete_user(user_id: int, request: Request):
    if user_id == current_user(request)['id']:
        raise HTTPException(400, "You can't remove your own login.")
    c = conn()
    try:
        row = c.execute(f'SELECT * FROM {T_USERS} WHERE id=%s', (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'User not found')
        if row['role'] == 'owner' and row['active'] and _owner_count(c) <= 1:
            raise HTTPException(400, 'There must always be at least one active owner.')
        c.execute(f'DELETE FROM {T_SESSIONS} WHERE user_id=%s', (user_id,))
        c.execute(f'DELETE FROM {T_USERS} WHERE id=%s', (user_id,))
        c.commit()
    finally:
        c.close()
    forget_sessions(user_id)
    audit_log.info('user_deleted id=%s by=%s', user_id, current_user(request)['id'])
    return {'ok': True}


@app.get('/')
def root():
    return FileResponse(STATIC / 'index.html')


# ---------- Health checks (for UptimeRobot / Render). No login needed, nothing private is returned. ----------
# GET and HEAD both work, because UptimeRobot's free plan often pings with HEAD.
@app.api_route('/health', methods=['GET', 'HEAD'])
def health():
    return JSONResponse({'status': 'ok'}, headers={'Cache-Control': 'no-store'})


@app.api_route('/health/db', methods=['GET', 'HEAD'])
def health_db():
    # Same as /health, but also checks the database (a daily-active ping also stops a free Supabase project from pausing).
    try:
        c = conn()
        try:
            c.execute('SELECT 1').fetchone()
        finally:
            c.close()
    except Exception as exc:
        log.error('Health check: database not reachable: %s', type(exc).__name__)   # details stay in the server log
        return JSONResponse({'status': 'error'}, status_code=503, headers={'Cache-Control': 'no-store'})
    return JSONResponse({'status': 'ok', 'database': 'ok'}, headers={'Cache-Control': 'no-store'})


@app.get('/static/{path:path}')
def static_files(path: str):
    # Only plain asset files (js, css, images, fonts) inside frontend/. No docs, config, dotfiles, maps or source files.
    if not core.static_path_allowed(path):
        raise HTTPException(404, 'Not found')
    base = STATIC.resolve()
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(404, 'Not found')
    # index.html links these with ?v=NN, so a changed file gets a new URL; the browser can keep them for an hour instead of re-asking every click/reload.
    return FileResponse(target, headers={'Cache-Control': 'public, max-age=3600'})


@app.get('/api/settings')
def get_settings():
    c = conn()
    try:
        return dict(c.execute('SELECT * FROM settings WHERE id=1').fetchone())
    finally:
        c.close()


@app.put('/api/settings', dependencies=OWNER)
def update_settings(payload: SettingsIn, request: Request):
    name = (payload.business_name or '').strip()
    if not name:
        raise HTTPException(400, 'Business name is required')
    c = conn()
    try:
        c.execute(
            'UPDATE settings SET business_name=%s,address=%s,phone=%s,gst_number=%s,food_license=%s,printer_name=%s,paper_size=%s,auto_print=%s,default_order_type=%s,tax_percent=%s,delivery_fee=%s,printer_mode=%s,printer_target=%s,printer_baudrate=%s WHERE id=1',
            (name, payload.address, payload.phone, payload.gst_number, payload.food_license.strip(), payload.printer_name,
             payload.paper_size, payload.auto_print, payload.default_order_type, payload.tax_percent,
             payload.delivery_fee, payload.printer_mode, payload.printer_target, payload.printer_baudrate)
        )
        c.commit()
    finally:
        c.close()
    audit_log.info('settings_updated by=%s', current_user(request)['id'])
    return get_settings()


@app.get('/api/categories')
def get_categories():
    c = conn()
    try:
        return [dict(r) for r in c.execute('SELECT * FROM categories ORDER BY sort_order, name')]
    finally:
        c.close()


@app.post('/api/categories', dependencies=OWNER)
def add_category(payload: CategoryIn):
    name = payload.name.strip()
    icon = payload.icon.strip()
    if not name:
        raise HTTPException(400, 'Category name is required')
    c = conn()
    try:
        max_order = c.execute('SELECT COALESCE(MAX(sort_order), -1) AS n FROM categories').fetchone()['n']
        row = c.execute('INSERT INTO categories(name,icon,sort_order) VALUES (%s,%s,%s) RETURNING id', (name, icon, max_order + 1)).fetchone()
        category_id = row['id']
        c.commit(); row = c.execute('SELECT * FROM categories WHERE id=%s', (category_id,)).fetchone()
        return dict(row)
    except errors.UniqueViolation:
        c.rollback()
        raise HTTPException(409, 'A category with this name already exists')
    finally:
        c.close()


@app.put('/api/categories/{category_id}', dependencies=OWNER)
def update_category(category_id: int, payload: CategoryIn):
    name = payload.name.strip(); icon = payload.icon.strip()
    if not name: raise HTTPException(400, 'Category name is required')
    c = conn()
    try:
        row = c.execute('SELECT * FROM categories WHERE id=%s', (category_id,)).fetchone()
        if not row: raise HTTPException(404, 'Category not found')
        old_name = row['name']
        c.execute('UPDATE categories SET name=%s, icon=%s WHERE id=%s', (name, icon, category_id))
        if old_name != name:
            c.execute('UPDATE menu_items SET category=%s WHERE category=%s', (name, old_name))
        c.commit()
        return dict(c.execute('SELECT * FROM categories WHERE id=%s', (category_id,)).fetchone())
    except errors.UniqueViolation:
        c.rollback()
        raise HTTPException(409, 'A category with this name already exists')
    finally:
        c.close()


@app.post('/api/categories/reorder', dependencies=OWNER)
def reorder_categories(payload: CategoryOrderIn):
    c = conn()
    try:
        existing = [r['id'] for r in c.execute('SELECT id FROM categories ORDER BY sort_order, id').fetchall()]
        requested = []
        seen = set()
        for cid in payload.category_ids:
            if cid in existing and cid not in seen:
                requested.append(cid); seen.add(cid)
        requested += [cid for cid in existing if cid not in seen]
        for idx, cid in enumerate(requested):
            c.execute('UPDATE categories SET sort_order=%s WHERE id=%s', (idx, cid))
        c.commit()
        return [dict(r) for r in c.execute('SELECT * FROM categories ORDER BY sort_order, name').fetchall()]
    finally:
        c.close()


@app.delete('/api/categories/{category_id}', dependencies=OWNER)
def delete_category(category_id: int, request: Request):
    c = conn()
    try:
        row = c.execute('SELECT * FROM categories WHERE id=%s', (category_id,)).fetchone()
        if not row: raise HTTPException(404, 'Category not found')
        count = c.execute('SELECT COUNT(*) AS n FROM menu_items WHERE category=%s', (row['name'],)).fetchone()['n']
        if count:
            raise HTTPException(409, f'Cannot delete {row["name"]} while it still has {count} menu item(s). Move or delete those items first.')
        c.execute('DELETE FROM categories WHERE id=%s', (category_id,)); c.commit()
    finally:
        c.close()
    audit_log.info('category_deleted id=%s by=%s', category_id, current_user(request)['id'])
    return {'ok': True}


@app.get('/api/menu-items')
def get_menu_items():
    c = conn()
    try:
        return [dict(r) for r in c.execute('SELECT * FROM menu_items ORDER BY category,name')]
    finally:
        c.close()


def _ensure_category(c, category: str):
    if not c.execute('SELECT 1 FROM categories WHERE name=%s', (category,)).fetchone():
        max_order = c.execute('SELECT COALESCE(MAX(sort_order), -1) AS n FROM categories').fetchone()['n']
        c.execute('INSERT INTO categories(name,icon,sort_order) VALUES (%s,%s,%s)', (category, '', max_order + 1))


@app.post('/api/menu-items', dependencies=OWNER)
def add_menu_item(payload: MenuItemIn, request: Request):
    category = payload.category.strip() or 'Other'
    c = conn()
    try:
        _ensure_category(c, category)
        r = c.execute('INSERT INTO menu_items(name,category,price,available) VALUES (%s,%s,%s,%s) RETURNING *', (payload.name.strip(), category, payload.price, payload.available)).fetchone()
        c.commit()
        return dict(r)
    finally:
        c.close()


@app.put('/api/menu-items/{item_id}')
def update_menu_item(item_id: int, payload: MenuItemIn, request: Request):
    user = current_user(request)
    category = payload.category.strip() or 'Other'
    name = payload.name.strip()
    c = conn()
    try:
        row = c.execute('SELECT * FROM menu_items WHERE id=%s', (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Menu item not found')
        if user.get('role') != 'owner':
            # Kitchen staff may switch a dish on/off ("sold out"). Names, categories and PRICES are the owner's.
            unchanged = (name == (row['name'] or '').strip() and category == ((row['category'] or '').strip() or 'Other')
                         and abs(float(row['price']) - float(payload.price)) < 0.005)
            if not unchanged:
                raise HTTPException(403, 'Staff can only switch a dish on or off. Ask the owner to change names, categories or prices.')
        else:
            _ensure_category(c, category)
        c.execute('UPDATE menu_items SET name=%s,category=%s,price=%s,available=%s WHERE id=%s', (name, category, payload.price, bool(payload.available), item_id))
        c.commit()
        r = c.execute('SELECT * FROM menu_items WHERE id=%s', (item_id,)).fetchone()
    finally:
        c.close()
    if float(row['price']) != float(r['price']):
        audit_log.info('menu_price_changed item=%s old=%s new=%s by=%s', item_id, row['price'], r['price'], user['id'])
    return dict(r)


@app.delete('/api/menu-items/{item_id}', dependencies=OWNER)
def delete_menu_item(item_id: int, request: Request):
    c = conn()
    try:
        c.execute('DELETE FROM menu_items WHERE id=%s', (item_id,)); c.commit()
    finally:
        c.close()
    audit_log.info('menu_item_deleted id=%s by=%s', item_id, current_user(request)['id'])
    return {'ok': True}


# ---------- Dining tables: the live table squares ----------
MAX_TABLES = 200
TABLES_NOT_READY = 'Tables are not set up yet. Apply migrations/0005_dining_tables.sql, then reload.'


def _next_table_label(labels) -> str:
    nums = [int(x.strip()) for x in labels if re.fullmatch(r'\s*[0-9]{1,6}\s*', x)]
    return str(max(nums) + 1 if nums else 1)


@app.get('/api/tables')
def get_tables():
    """Every table, with the orders of its CURRENT sitting (not cancelled, newer than the last time the table was freed)."""
    c = conn()
    try:
        tables = c.execute('SELECT id, label FROM dining_tables ORDER BY sort_order, id').fetchall()
        rows = c.execute(
            "SELECT t.id AS table_id, o.id, o.order_number, o.status, o.total, o.created_at, "
            "EXTRACT(EPOCH FROM (now() - o.created_at)) AS age_s "
            "FROM dining_tables t JOIN orders o ON lower(btrim(o.table_number)) = lower(btrim(t.label)) "
            "WHERE o.status <> 'Cancelled' AND o.id > t.cleared_after_order_id ORDER BY o.id").fetchall()
    except errors.UndefinedTable:
        raise HTTPException(503, TABLES_NOT_READY)
    finally:
        c.close()
    by_table = {}
    for r in rows:
        by_table.setdefault(r['table_id'], []).append(r)
    out = []
    for t in tables:
        orders = by_table.get(t['id'], [])
        out.append({
            'id': t['id'], 'label': t['label'], 'occupied': bool(orders), 'order_count': len(orders),
            'total': round(sum(float(o['total'] or 0) for o in orders), 2),
            'since_seconds': max(0, int(float(orders[0]['age_s']))) if orders else None,
            'orders': [{'id': o['id'], 'order_number': o['order_number'], 'status': o['status'], 'total': float(o['total'] or 0), 'created_at': o['created_at']} for o in orders],
        })
    return out


@app.post('/api/tables', dependencies=OWNER)
def add_table(payload: TableIn, request: Request):
    label = ' '.join(payload.label.split())   # trims and collapses spaces
    if any(ord(ch) < 32 for ch in label):
        raise HTTPException(400, 'The table name has characters that are not allowed')
    c = conn()
    try:
        for _ in range(3):
            rows = c.execute('SELECT label, sort_order FROM dining_tables').fetchall()
            if len(rows) >= MAX_TABLES:
                raise HTTPException(400, f'You can have at most {MAX_TABLES} tables.')
            name = label or _next_table_label([r['label'] for r in rows])
            order = max([r['sort_order'] for r in rows], default=0) + 1
            try:
                row = c.execute('INSERT INTO dining_tables(label, sort_order, cleared_after_order_id) VALUES (%s,%s,(SELECT COALESCE(MAX(id),0) FROM orders)) RETURNING id, label', (name, order)).fetchone()
                c.commit()
                audit_log.info('table_added id=%s label=%s by=%s', row['id'], row['label'], current_user(request)['id'])
                return {'id': row['id'], 'label': row['label']}
            except errors.UniqueViolation:
                c.rollback()
                if label:
                    raise HTTPException(409, f'Table {label} already exists')
                # automatic number: another device took it a moment ago, so look again
        raise HTTPException(409, 'Could not add the table, please try again')
    except errors.UndefinedTable:
        c.rollback()
        raise HTTPException(503, TABLES_NOT_READY)
    finally:
        c.close()


@app.post('/api/tables/{table_id}/free')
def free_table(table_id: int, request: Request):
    """The guests have left: later orders for this table start a new sitting. Orders and reports are untouched."""
    c = conn()
    try:
        row = c.execute('UPDATE dining_tables SET cleared_after_order_id = (SELECT COALESCE(MAX(id),0) FROM orders) WHERE id=%s RETURNING id, label', (table_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Table not found')
        c.commit()
    except errors.UndefinedTable:
        c.rollback()
        raise HTTPException(503, TABLES_NOT_READY)
    finally:
        c.close()
    audit_log.info('table_freed id=%s label=%s by=%s', row['id'], row['label'], current_user(request)['id'])
    return {'ok': True}


@app.delete('/api/tables/{table_id}', dependencies=OWNER)
def delete_table(table_id: int, request: Request):
    c = conn()
    try:
        t = c.execute('SELECT id, label FROM dining_tables WHERE id=%s', (table_id,)).fetchone()
        if not t:
            raise HTTPException(404, 'Table not found')
        busy = c.execute(
            "SELECT COUNT(*) AS n FROM orders o JOIN dining_tables d ON lower(btrim(o.table_number)) = lower(btrim(d.label)) "
            "WHERE d.id=%s AND o.status <> 'Cancelled' AND o.id > d.cleared_after_order_id", (table_id,)).fetchone()['n']
        if busy:
            raise HTTPException(409, f'Table {t["label"]} still has {busy} order(s). Free the table first.')
        c.execute('DELETE FROM dining_tables WHERE id=%s', (table_id,))
        c.commit()
    except errors.UndefinedTable:
        c.rollback()
        raise HTTPException(503, TABLES_NOT_READY)
    finally:
        c.close()
    audit_log.info('table_deleted id=%s label=%s by=%s', t['id'], t['label'], current_user(request)['id'])
    return {'ok': True}


@app.get('/api/next-order-number')
def next_order_number():
    """A PREVIEW for the screen (the bill may be printed before the save returns). The server assigns the real number on save."""
    c = conn()
    try:
        return {'next': c.execute('SELECT COALESCE(MAX(order_number),1000)+1 AS n FROM orders').fetchone()['n']}
    finally:
        c.close()


def _month_or_400(month: Optional[str]):
    try:
        core.parse_month(month)
        return core.month_bounds_utc(month)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _like_escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


@app.get('/api/orders')
def get_orders(status: Optional[Literal['New', 'Accepted', 'Preparing', 'Ready', 'Completed', 'Cancelled']] = None,
               q: Optional[str] = Query(default=None, max_length=64),
               month: Optional[str] = Query(default=None, pattern=r'^\d{4}-\d{2}$'),
               limit: int = Query(default=1000, ge=1, le=5000), offset: int = Query(default=0, ge=0, le=1_000_000)):
    """Orders of one India-local month, newest first. At most `limit` rows per request (page with `offset`)."""
    start, end = _month_or_400(month)
    sql = 'SELECT * FROM orders WHERE created_at >= %s AND created_at < %s'; args = [start, end]
    if status: sql += ' AND status=%s'; args.append(status)
    if q:
        like = f'%{_like_escape(q)}%'
        sql += " AND (CAST(order_number AS TEXT) LIKE %s ESCAPE '\\' OR customer_name LIKE %s ESCAPE '\\' OR phone LIKE %s ESCAPE '\\')"
        args += [like, like, like]
    sql += ' ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s'; args += [limit, offset]
    c = conn()
    try:
        return serialize_orders(c, c.execute(sql, args).fetchall())
    finally:
        c.close()


@app.get('/api/orders/{order_id}')
def get_order(order_id: int):
    c = conn()
    try:
        r = c.execute('SELECT * FROM orders WHERE id=%s', (order_id,)).fetchone()
        if not r: raise HTTPException(404, 'Order not found')
        return serialize_orders(c, [r])[0]
    finally:
        c.close()


ORDER_NUMBER_LOCK = 7_240_001   # pg_advisory_xact_lock key: serialises "read the highest number, add one, insert"


def _order_with_items(c, row) -> dict:
    d = dict(row)
    d['items'] = [dict(r) for r in c.execute('SELECT menu_item_id,item_name,quantity,unit_price FROM order_items WHERE order_id=%s ORDER BY id', (d['id'],)).fetchall()]
    return d


def _truthy_db(value) -> bool:
    return bool(value) if isinstance(value, (bool, int)) else str(value).lower() in ('t', 'true', '1')


@app.post('/api/orders')
def create_order(payload: OrderIn, request: Request):
    method = 'UPI' if str(payload.payment_method or '').strip().upper() == 'UPI' else 'Cash'
    rid = payload.client_request_id if SCHEMA_FEATURES['client_request_id'] else None
    c = conn()
    try:
        settings = c.execute('SELECT * FROM settings WHERE id=1').fetchone()
        live_auto_print = _truthy_db(settings.get('auto_print'))   # rides along, so a phone with old settings prints correctly
        if rid:
            again = c.execute('SELECT * FROM orders WHERE client_request_id=%s', (rid,)).fetchone()
            if again:   # same "Confirm" sent twice (lost reply / double tap): return the order that already exists
                d = _order_with_items(c, again); d['auto_print'] = live_auto_print
                return d
        ids = sorted({i.menu_item_id for i in payload.items})
        by_id = {r['id']: r for r in c.execute('SELECT * FROM menu_items WHERE id = ANY(%s)', (ids,)).fetchall()}
        if any(i.menu_item_id not in by_id for i in payload.items): raise HTTPException(400, 'Invalid menu item')
        delivery = payload.delivery_fee
        if delivery == 0 and payload.order_type == 'Delivery': delivery = float(settings['delivery_fee'] or 0)
        try:
            t = core.compute_totals([(by_id[i.menu_item_id]['price'], i.quantity) for i in payload.items], settings['tax_percent'], delivery)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        created = core.db_timestamp(core.utc_now().replace(microsecond=0))
        cols = 'order_number,customer_name,phone,address,table_number,order_type,subtotal,delivery_fee,tax,total,special_instructions,payment_method,payment_status,status,created_at'
        for attempt in range(3):
            # The lock is held until commit/rollback, so two phones confirming at once get consecutive numbers, never the same one.
            c.execute('SELECT pg_advisory_xact_lock(%s)', (ORDER_NUMBER_LOCK,))
            next_num = c.execute('SELECT COALESCE(MAX(order_number),1000)+1 AS n FROM orders').fetchone()['n']
            vals = [next_num, payload.customer_name.strip(), payload.phone.strip(), payload.address.strip(), payload.table_number.strip(), payload.order_type,
                    float(t['subtotal']), float(t['delivery_fee']), float(t['tax']), float(t['total']), payload.special_instructions, method, 'Paid', 'Completed', created]
            if rid:
                cols_sql, marks, vals = cols + ',client_request_id', ','.join(['%s'] * 16), vals + [rid]
            else:
                cols_sql, marks = cols, ','.join(['%s'] * 15)
            try:
                d = dict(c.execute(f'INSERT INTO orders({cols_sql}) VALUES ({marks}) RETURNING *', vals).fetchone())
                c.executemany(
                    'INSERT INTO order_items(order_id,menu_item_id,item_name,quantity,unit_price) VALUES (%s,%s,%s,%s,%s)',
                    [(d['id'], by_id[i.menu_item_id]['id'], by_id[i.menu_item_id]['name'], i.quantity, by_id[i.menu_item_id]['price']) for i in payload.items]
                )
                c.commit()   # order + items become visible together, or not at all
                break
            except errors.UniqueViolation:
                c.rollback()   # a duplicate request id (already saved) or, rarely, a number clash: look again, never drop the order
                if rid:
                    again = c.execute('SELECT * FROM orders WHERE client_request_id=%s', (rid,)).fetchone()
                    if again:
                        d = _order_with_items(c, again); d['auto_print'] = live_auto_print
                        return d
                if attempt == 2:
                    raise
        d['items'] = [{'menu_item_id': i.menu_item_id, 'item_name': by_id[i.menu_item_id]['name'], 'quantity': i.quantity, 'unit_price': by_id[i.menu_item_id]['price']} for i in payload.items]
        d['auto_print'] = live_auto_print
        return d
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


@app.put('/api/orders/{order_id}')
def update_order(order_id: int, payload: OrderUpdateIn):
    sent = payload.model_dump(exclude_none=True)
    c = conn()
    try:
        current = c.execute('SELECT * FROM orders WHERE id=%s', (order_id,)).fetchone()
        if not current:
            raise HTTPException(404, 'Order not found')
        simple = ('customer_name', 'phone', 'address', 'order_type', 'payment_status', 'special_instructions', 'status', 'payment_method')
        changes = {k: (v.strip() if isinstance(v, str) and k in ('customer_name', 'phone', 'address') else v) for k, v in sent.items() if k in simple}
        if 'payment_method' in changes:
            changes['payment_method'] = 'UPI' if str(changes['payment_method']).strip().upper() == 'UPI' else 'Cash'
            changes['payment_status'] = 'Paid'
        if payload.items is not None:
            if not payload.items:
                raise HTTPException(400, 'Keep at least one item in the order')
            ids = sorted({i.menu_item_id for i in payload.items})
            by_id = {r['id']: r for r in c.execute('SELECT * FROM menu_items WHERE id = ANY(%s)', (ids,)).fetchall()}
            if any(i.menu_item_id not in by_id for i in payload.items):
                raise HTTPException(400, 'One or more menu items are invalid')
            settings = c.execute('SELECT * FROM settings WHERE id=1').fetchone()
            delivery = payload.delivery_fee if payload.delivery_fee is not None else float(current['delivery_fee'] or 0)
            if delivery == 0 and changes.get('order_type', current['order_type']) == 'Delivery':
                delivery = float(settings['delivery_fee'] or 0)
            try:
                t = core.compute_totals([(by_id[i.menu_item_id]['price'], i.quantity) for i in payload.items], settings['tax_percent'], delivery)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            c.execute('DELETE FROM order_items WHERE order_id=%s', (order_id,))
            c.executemany('INSERT INTO order_items(order_id,menu_item_id,item_name,quantity,unit_price) VALUES (%s,%s,%s,%s,%s)',
                          [(order_id, by_id[i.menu_item_id]['id'], by_id[i.menu_item_id]['name'], i.quantity, by_id[i.menu_item_id]['price']) for i in payload.items])
            changes.update({'subtotal': float(t['subtotal']), 'delivery_fee': float(t['delivery_fee']), 'tax': float(t['tax']), 'total': float(t['total'])})
        elif payload.delivery_fee is not None:
            raise HTTPException(400, 'The delivery fee can only be changed together with the items.')
        if not changes:
            raise HTTPException(400, 'No supported changes')
        # Column names come from the fixed list above, never from the request.
        c.execute(f'UPDATE orders SET {", ".join(f"{k}=%s" for k in changes)} WHERE id=%s', (*changes.values(), order_id))
        c.commit()   # items delete/insert and the totals commit together
        return _order_with_items(c, c.execute('SELECT * FROM orders WHERE id=%s', (order_id,)).fetchone())
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


@app.delete('/api/orders/{order_id}', dependencies=OWNER)
def delete_order(order_id: int, request: Request):
    c = conn()
    try:
        row = c.execute('SELECT order_number,total,status FROM orders WHERE id=%s', (order_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Order not found')
        c.execute('DELETE FROM order_items WHERE order_id=%s', (order_id,))
        c.execute('DELETE FROM orders WHERE id=%s', (order_id,))
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    audit_log.info('order_deleted id=%s number=%s total=%s status=%s by=%s', order_id, row['order_number'], row['total'], row['status'], current_user(request)['id'])
    return {'ok': True}


class ClearOrdersIn(BaseModel):
    confirm: str = Field(default='', max_length=20)


@app.post('/api/orders/clear', dependencies=OWNER)
def clear_orders(payload: ClearOrdersIn, request: Request):
    # Start again from zero orders. Menu, categories, inventory and settings are kept.
    if payload.confirm != 'RESET':
        raise HTTPException(400, 'Type RESET to confirm')
    c = conn()
    try:
        n = c.execute('SELECT COUNT(*) AS n FROM orders').fetchone()['n']
        c.execute('DELETE FROM order_items')
        c.execute('DELETE FROM orders')
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    audit_log.warning('orders_cleared count=%s by=%s', n, current_user(request)['id'])
    return {'ok': True, 'deleted': n}


@app.get('/api/reports', dependencies=OWNER)
def reports(month: Optional[str] = Query(default=None, pattern=r'^\d{4}-\d{2}$')):
    start, end = _month_or_400(month)
    month = month or core.current_month_ist()
    c = conn()
    try:
        orders = c.execute('SELECT status, total FROM orders WHERE created_at >= %s AND created_at < %s', (start, end)).fetchall()
        completed = [o for o in orders if o['status'] == 'Completed']
        revenue = sum(float(o['total'] or 0) for o in completed)
        avg = (revenue / len(completed)) if completed else 0
        counts = {}
        for r in c.execute("SELECT item_name, SUM(quantity) qty FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.created_at >= %s AND o.created_at < %s AND o.status != 'Cancelled' GROUP BY item_name ORDER BY qty DESC LIMIT 5", (start, end)).fetchall():
            counts[r['item_name']] = r['qty']
    finally:
        c.close()
    return {'month': month, 'revenue': round(revenue, 2), 'orders': len(orders), 'average_order': round(avg, 2), 'most_ordered': list(counts.items())[:5], 'completed': len(completed), 'cancelled': len([o for o in orders if o['status'] == 'Cancelled'])}


@app.get('/api/inventory', dependencies=OWNER)
def get_inventory(month: Optional[str] = Query(default=None, pattern=r'^\d{4}-\d{2}$'), q: Optional[str] = Query(default=None, max_length=64),
                  limit: int = Query(default=1000, ge=1, le=5000), offset: int = Query(default=0, ge=0, le=1_000_000)):
    _month_or_400(month)
    month = month or core.current_month_ist()
    c = conn()
    try:
        sql = "SELECT * FROM inventory_purchases WHERE purchase_date::text LIKE %s"
        args = [month + '%']
        if q:
            sql += " AND (item_name ILIKE %s ESCAPE '\\' OR category ILIKE %s ESCAPE '\\')"; like = f'%{_like_escape(q)}%'; args.extend([like, like])
        sql += ' ORDER BY purchase_date DESC, id DESC LIMIT %s OFFSET %s'; args += [limit, offset]
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
        total = c.execute("SELECT COALESCE(SUM(total_cost),0) AS t FROM inventory_purchases WHERE purchase_date::text LIKE %s", (month + '%',)).fetchone()['t']
        by_item = [dict(r) for r in c.execute(
            "SELECT item_name, category, unit, ROUND(SUM(quantity)::numeric,2) quantity, ROUND(SUM(total_cost)::numeric,2) total_cost FROM inventory_purchases WHERE purchase_date::text LIKE %s GROUP BY item_name, category, unit ORDER BY total_cost DESC LIMIT 500",
            (month + '%',)
        ).fetchall()]
    finally:
        c.close()
    return {'month': month, 'total_spend': round(float(total), 2), 'purchases': rows, 'by_item': by_item}


@app.get('/api/inventory/items', dependencies=OWNER)
def inventory_items():
    """Every item ever bought (all months), for the one-tap stock buttons and the all-time spending ranking."""
    c = conn()
    try:
        rows = c.execute('SELECT id, item_name, category, unit, quantity, total_cost, purchase_date::text AS purchase_date FROM inventory_purchases ORDER BY purchase_date DESC, id DESC LIMIT 20000').fetchall()
    finally:
        c.close()
    items = {}
    for r in rows:
        name = (r['item_name'] or '').strip()
        cat = (r['category'] or 'Other').strip() or 'Other'
        key = (name.lower(), cat.lower())
        it = items.get(key)
        if it is None:   # rows are newest first, so the first one seen is the latest purchase
            it = items[key] = {'item_name': name, 'category': cat, 'last_date': r['purchase_date'], 'last_total': float(r['total_cost']), 'last_unit': r['unit'], 'times_bought': 0, 'all_time_total': 0.0}
        it['times_bought'] += 1
        it['all_time_total'] = round(it['all_time_total'] + float(r['total_cost']), 2)
    return sorted(items.values(), key=lambda x: (x['category'].lower(), x['item_name'].lower()))


@app.post('/api/inventory', dependencies=OWNER)
def add_inventory(payload: InventoryPurchaseIn):
    date = clean_purchase_date(payload.purchase_date)
    unit_price, total = purchase_numbers(payload)
    c = conn()
    try:
        r = c.execute(
            'INSERT INTO inventory_purchases(item_name,category,quantity,unit,unit_price,total_cost,purchase_date,notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
            (payload.item_name.strip(), payload.category.strip() or 'Raw Material', payload.quantity, payload.unit.strip() or 'kg', unit_price, total, date, payload.notes.strip())
        ).fetchone()
        c.commit()
        return dict(r)
    finally:
        c.close()


@app.put('/api/inventory/{purchase_id}', dependencies=OWNER)
def update_inventory(purchase_id: int, payload: InventoryPurchaseIn):
    date = clean_purchase_date(payload.purchase_date)
    unit_price, total = purchase_numbers(payload)
    c = conn()
    try:
        if not c.execute('SELECT 1 FROM inventory_purchases WHERE id=%s', (purchase_id,)).fetchone():
            raise HTTPException(404, 'Inventory purchase not found')
        r = c.execute(
            'UPDATE inventory_purchases SET item_name=%s,category=%s,quantity=%s,unit=%s,unit_price=%s,total_cost=%s,purchase_date=%s,notes=%s WHERE id=%s RETURNING *',
            (payload.item_name.strip(), payload.category.strip() or 'Raw Material', payload.quantity, payload.unit.strip() or 'kg', unit_price, total, date, payload.notes.strip(), purchase_id)
        ).fetchone()
        c.commit()
        return dict(r)
    finally:
        c.close()


@app.delete('/api/inventory/{purchase_id}', dependencies=OWNER)
def delete_inventory(purchase_id: int, request: Request):
    c = conn()
    try:
        c.execute('DELETE FROM inventory_purchases WHERE id=%s', (purchase_id,)); c.commit()
    finally:
        c.close()
    audit_log.info('inventory_deleted id=%s by=%s', purchase_id, current_user(request)['id'])
    return {'ok': True}


@app.post('/api/demo/reset', dependencies=OWNER)
def reset_demo(request: Request):
    # Destructive, so it exists ONLY in a non-production environment with SEED_DEMO_DATA=true.
    if not DEMO_ENABLED:
        raise HTTPException(403, 'Demo reset is disabled.')
    c = conn()
    try:
        c.execute('DELETE FROM order_items'); c.execute('DELETE FROM orders'); c.execute('DELETE FROM inventory_purchases'); c.commit()
        seed_orders(c); seed_inventory(c); c.commit()
    finally:
        c.close()
    audit_log.warning('demo_reset by=%s', current_user(request)['id'])
    return {'ok': True}


@app.get('/api/print/{order_id}')
def print_order(order_id: int):
    c = conn(); r = c.execute('SELECT * FROM orders WHERE id=%s', (order_id,)).fetchone(); c.close()
    if not r: raise HTTPException(404, 'Order not found')
    return serialize_order(r)


@app.post('/api/printers/print-batch')
def hardware_print_batch(payload: HardwarePrintBatchIn, request: Request):
    if not payload.order_ids:
        raise HTTPException(400, 'No orders selected')
    c = conn()
    try:
        settings_row = c.execute('SELECT * FROM settings WHERE id=1').fetchone()
        owner = current_user(request).get('role') == 'owner'   # only the owner may name a printer / port; everyone else uses the saved settings
        mode = (payload.mode if owner else '') or settings_row['printer_mode'] or 'browser'
        target = (payload.target if owner else '') or settings_row['printer_target'] or ''
        rows = c.execute('SELECT * FROM orders WHERE id = ANY(%s)', (payload.order_ids,)).fetchall()
        by_id = {r['id']: r for r in rows}
        missing = [oid for oid in payload.order_ids if oid not in by_id]
        if missing:
            raise HTTPException(404, f'Order not found: {missing[0]}')
        item_rows = c.execute(
            'SELECT order_id,menu_item_id,item_name,quantity,unit_price FROM order_items WHERE order_id = ANY(%s) ORDER BY order_id,id',
            (payload.order_ids,)
        ).fetchall()
        grouped = {}
        for item in item_rows:
            grouped.setdefault(item['order_id'], []).append(dict(item))
        orders = []
        for oid in payload.order_ids:
            d = dict(by_id[oid])
            d['items'] = grouped.get(oid, [])
            orders.append(d)
        settings = dict(settings_row)
    finally:
        c.close()

    if mode == 'browser':
        return {'ok': True, 'mode': 'browser', 'message': 'Use browser print preview.', 'order_ids': payload.order_ids}

    try:
        if mode == 'windows' and target:
            for order in orders:
                send_windows_raw(target, escpos_order_bytes(order, settings))
        elif mode == 'serial' and target:
            for order in orders:
                send_serial(target, escpos_order_bytes(order, settings), int(settings.get('printer_baudrate') or 9600))
        else:
            raise ValueError('Printer mode or target is not configured')
    except Exception:
        log.exception('Batch print failed (mode=%s)', mode)
        raise HTTPException(400, 'Could not print. Check that the printer is on and connected, then try again.')
    return {'ok': True, 'mode': mode, 'target': target, 'count': len(orders), 'message': 'Order batch sent successfully.'}


@app.get('/api/printers')
def list_printers():
    printers = []
    ports = []
    # Windows printer queues (Bluetooth/USB printers installed with a driver usually appear here).
    try:
        import win32print
        for p in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS, None, 2):
            printers.append({'name': p.get('pPrinterName'), 'driver': p.get('pDriverName', ''), 'port': p.get('pPortName', ''), 'kind': 'windows'})
    except Exception:
        pass
    # Bluetooth SPP devices and USB serial devices often appear as COM ports.
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            ports.append({'name': p.description or p.device, 'port': p.device, 'manufacturer': p.manufacturer or '', 'kind': 'serial'})
    except Exception:
        pass
    return {'printers': printers, 'ports': ports, 'platform': os.name}


def send_windows_raw(printer_name: str, data: bytes):
    import win32print
    handle = win32print.OpenPrinter(printer_name)
    try:
        job = win32print.StartDocPrinter(handle, 1, (printer_name, None, 'RAW'))
        try:
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, data)
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
    finally:
        win32print.ClosePrinter(handle)
    return job


def send_serial(port: str, data: bytes, baudrate: int = 9600):
    import serial
    with serial.Serial(port=port, baudrate=baudrate, timeout=2, write_timeout=2) as s:
        s.write(data)
        s.flush()


@app.post('/api/printers/test', dependencies=OWNER)
def test_hardware_print():
    c = conn(); settings = dict(c.execute('SELECT * FROM settings WHERE id=1').fetchone()); c.close()
    mode = settings.get('printer_mode') or 'browser'; target = settings.get('printer_target') or ''
    if mode == 'browser':
        return {'ok': True, 'mode': 'browser', 'message': 'Browser print fallback selected. Use the Settings > Printer Center test print.'}
    data = escpos_bytes(business_name(settings) + '\n' + 'THERMAL PRINTER TEST\n' + '-'*32 + '\n58mm PAPER\nPrinter connection OK\n\n')
    try:
        if mode == 'windows' and target:
            send_windows_raw(target, data)
        elif mode == 'serial' and target:
            send_serial(target, data, int(settings.get('printer_baudrate') or 9600))
        else:
            raise ValueError('Printer mode or target is not configured')
    except Exception:
        log.exception('Printer test failed (mode=%s)', mode)
        raise HTTPException(400, 'The printer test failed. Check that the printer is on and connected.')
    return {'ok': True, 'mode': mode, 'target': target, 'message': 'Printer test sent successfully.'}


@app.post('/api/printers/print')
def hardware_print(payload: HardwarePrintIn, request: Request):
    c = conn(); order_row = c.execute('SELECT * FROM orders WHERE id=%s', (payload.order_id,)).fetchone(); settings_row = c.execute('SELECT * FROM settings WHERE id=1').fetchone(); c.close()
    if not order_row: raise HTTPException(404, 'Order not found')
    order = serialize_order(order_row)
    settings = dict(settings_row)
    owner = current_user(request).get('role') == 'owner'
    mode = (payload.mode if owner else '') or settings.get('printer_mode') or 'browser'
    target = (payload.target if owner else '') or settings.get('printer_target') or ''
    text = print_ticket_text(order, settings)
    if mode == 'browser':
        return {'ok': True, 'mode': 'browser', 'message': 'Use the browser print preview.', 'text': text}
    data = escpos_order_bytes(order, settings)
    try:
        if mode == 'windows' and target:
            send_windows_raw(target, data)
        elif mode == 'serial' and target:
            send_serial(target, data, int(settings.get('printer_baudrate') or 9600))
        else:
            raise ValueError('Printer mode or target is not configured')
    except Exception:
        log.exception('Print failed (mode=%s)', mode)
        raise HTTPException(400, 'Could not print. Check that the printer is on and connected, then try again.')
    return {'ok': True, 'mode': mode, 'target': target, 'message': 'Print job sent successfully.'}
    return {'ok': True, 'mode': mode, 'target': target, 'message': 'Print job sent successfully.'}