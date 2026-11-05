"""Shared fixtures for the API integration tests.

These tests need FastAPI + psycopg installed and a DISPOSABLE PostgreSQL database:
    export TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/ruchira_test      # NEVER a live database
    pip install -r requirements-dev.txt && pytest -v
Without TEST_DATABASE_URL every test in test_api_*.py is skipped. The fixture refuses a URL that does not look like a test database.
"""
import os, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))
URL = os.getenv('TEST_DATABASE_URL', '').strip()


def pytest_collection_modifyitems(config, items):
    if URL:
        return
    skip = pytest.mark.skip(reason='TEST_DATABASE_URL not set (needs a disposable PostgreSQL database)')
    for item in items:
        if 'test_api_' in item.nodeid:
            item.add_marker(skip)


@pytest.fixture(scope='session')
def appmod():
    if not URL:
        pytest.skip('TEST_DATABASE_URL not set')
    if not any(w in URL.lower() for w in ('test', 'localhost', '127.0.0.1')) or 'supabase' in URL.lower():
        pytest.exit('Refusing to run: TEST_DATABASE_URL must point at a disposable test database (name contains "test" or host is localhost).')
    import psycopg
    with psycopg.connect(URL, autocommit=True) as c:   # fresh schema for every test session
        c.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public;')
        for f in ('0001_baseline_schema.sql', '0003_order_integrity.sql', '0005_dining_tables.sql'):   # 0002 needs Supabase roles; it guards for their absence but is not needed here
            c.execute((ROOT / 'migrations' / f).read_text(encoding='utf-8'))
    os.environ.update(DATABASE_URL=URL, APP_ENV='test', ADMIN_USERNAME='owner', ADMIN_PASSWORD='Correct-Horse-9', SEED_DEMO_DATA='false', TRUSTED_PROXY_HOPS='1')
    import importlib, app as mod
    importlib.reload(mod)
    mod.init_db()
    return mod


@pytest.fixture(scope='session')
def client(appmod):
    from fastapi.testclient import TestClient
    return TestClient(appmod.app, base_url='https://testserver')


def _login(appmod, username, password, ip):
    from fastapi.testclient import TestClient
    c = TestClient(appmod.app, base_url='https://testserver', headers={'X-Forwarded-For': ip})
    r = c.post('/api/login', json={'username': username, 'password': password})
    return c, r


@pytest.fixture(scope='session')
def owner(appmod):
    c, r = _login(appmod, 'owner', 'Correct-Horse-9', '203.0.113.1')
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(scope='session')
def staff(appmod, owner):
    r = owner.post('/api/users', json={'username': 'cook', 'name': 'Cook', 'password': 'Temporary-Pass-1', 'role': 'staff'})
    assert r.status_code == 200, r.text
    c, r = _login(appmod, 'cook', 'Temporary-Pass-1', '203.0.113.2')
    assert r.json()['must_change_password'] is True
    ok = c.post('/api/me/password', json={'current_password': 'Temporary-Pass-1', 'new_password': 'Kitchen-Secret-77'})
    assert ok.status_code == 200, ok.text
    return c


@pytest.fixture()
def menu_item(owner):
    r = owner.post('/api/menu-items', json={'name': 'Test Dish', 'category': 'Test', 'price': 100, 'available': True})
    assert r.status_code == 200, r.text
    return r.json()
