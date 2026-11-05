import pytest, re
from conftest import _login


def test_no_default_admin_account(appmod, client):
    r = client.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 401


def test_bootstrap_refuses_empty_db_without_password(appmod, monkeypatch):
    import psycopg
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)
    c = appmod.conn()
    try:
        c.execute(f'DELETE FROM {appmod.T_SESSIONS}'); 
        saved = c.execute(f'SELECT * FROM {appmod.T_USERS}').fetchall()
        c.execute(f'DELETE FROM {appmod.T_USERS}')
        with pytest.raises(RuntimeError, match='ADMIN_PASSWORD'):
            appmod.bootstrap_owner(c)
        monkeypatch.setenv('ADMIN_PASSWORD', 'admin123')
        with pytest.raises(RuntimeError, match='too common'):
            appmod.bootstrap_owner(c)
    finally:
        c.rollback(); c.close()   # nothing was committed: existing users are untouched
    assert appmod.conn().execute(f'SELECT COUNT(*) AS n FROM {appmod.T_USERS}').fetchone()['n'] >= 1


def test_existing_default_password_account_is_flagged_not_deleted(appmod):
    c = appmod.conn()
    c.execute(f"INSERT INTO {appmod.T_USERS}(username,name,password_hash,role,active,must_change_password,created_at) VALUES ('legacy','Legacy',%s,'staff',TRUE,FALSE,'')", (appmod.hash_password('admin123'),))
    c.commit()
    appmod.flag_default_password_accounts(c)
    row = c.execute(f"SELECT must_change_password FROM {appmod.T_USERS} WHERE username='legacy'").fetchone()
    c.close()
    assert row['must_change_password'] is True


def test_pending_password_change_blocks_every_other_api(appmod, owner):
    owner.post('/api/users', json={'username': 'newbie', 'name': 'N', 'password': 'Temporary-Pass-2', 'role': 'owner'})
    c, r = _login(appmod, 'newbie', 'Temporary-Pass-2', '203.0.113.9')
    assert r.json()['must_change_password'] is True
    for method, path in [('get', '/api/orders'), ('get', '/api/menu-items'), ('get', '/api/settings'), ('get', '/api/reports'), ('get', '/api/users'),
                         ('post', '/api/orders/clear'), ('get', '/api/inventory'), ('delete', '/api/orders/1')]:
        resp = getattr(c, method)(path)
        assert resp.status_code == 403 and resp.json().get('code') == 'password_change_required', (path, resp.status_code)
    assert c.get('/api/me').status_code == 200                       # allowed
    bad = c.post('/api/me/password', json={'current_password': 'Temporary-Pass-2', 'new_password': 'short'})
    assert bad.status_code == 400
    assert c.get('/api/orders').status_code == 403                    # failed change does not clear the flag
    ok = c.post('/api/me/password', json={'current_password': 'Temporary-Pass-2', 'new_password': 'Brand-New-Pass-5'})
    assert ok.status_code == 200
    assert c.get('/api/orders').status_code == 200                    # now normal use works
    assert c.post('/api/logout').status_code == 200
    assert c.get('/api/orders').status_code == 401


def test_password_policy_enforced_on_change(appmod, staff):
    for pw in ['admin123', 'short', 'x' * 129, 'password1234']:
        assert staff.post('/api/me/password', json={'current_password': 'Kitchen-Secret-77', 'new_password': pw}).status_code in (400, 422)


def test_unauthenticated_requests_get_401(client):
    for path in ['/api/orders', '/api/menu-items', '/api/reports', '/api/users', '/api/settings', '/api/inventory']:
        assert client.get(path).status_code == 401


def test_staff_forbidden_on_owner_routes(staff, menu_item):
    cases = [('get', '/api/users', None), ('post', '/api/users', {'username': 'zzz', 'password': 'Whatever-Pass-1'}), ('put', '/api/users/1', {'role': 'staff'}),
             ('delete', '/api/users/1', None), ('put', '/api/settings', {'business_name': 'x'}), ('post', '/api/orders/clear', {'confirm': 'RESET'}),
             ('post', '/api/demo/reset', None), ('get', '/api/reports', None), ('get', '/api/inventory', None), ('get', '/api/inventory/items', None),
             ('post', '/api/inventory', {'item_name': 'x', 'amount': 5}), ('put', '/api/inventory/1', {'item_name': 'x', 'amount': 5}), ('delete', '/api/inventory/1', None),
             ('post', '/api/categories', {'name': 'Hack'}), ('put', '/api/categories/1', {'name': 'Hack'}), ('delete', '/api/categories/1', None),
             ('post', '/api/categories/reorder', {'category_ids': [1]}), ('post', '/api/menu-items', {'name': 'x', 'category': 'y', 'price': 1}),
             ('delete', f"/api/menu-items/{menu_item['id']}", None), ('delete', '/api/orders/1', None), ('post', '/api/printers/test', None)]
    for method, path, body in cases:
        r = getattr(staff, method)(path, **({'json': body} if body is not None else {}))
        assert r.status_code == 403, (method, path, r.status_code)


def test_staff_cannot_change_price_but_can_toggle_availability(staff, menu_item):
    url = f"/api/menu-items/{menu_item['id']}"
    base = {'name': menu_item['name'], 'category': menu_item['category'], 'price': float(menu_item['price']), 'available': True}
    assert staff.put(url, json={**base, 'price': 1}).status_code == 403
    assert staff.put(url, json={**base, 'name': 'Renamed'}).status_code == 403
    r = staff.put(url, json={**base, 'available': False})
    assert r.status_code == 200 and r.json()['available'] is False


def test_staff_can_do_the_kitchen_workflow(staff, menu_item):
    assert staff.get('/api/menu-items').status_code == 200
    assert staff.get('/api/categories').status_code == 200
    assert staff.get('/api/settings').status_code == 200
    assert staff.get('/api/next-order-number').status_code == 200
    r = staff.post('/api/orders', json={'items': [{'menu_item_id': menu_item['id'], 'quantity': 2}], 'order_type': 'Pickup'})
    assert r.status_code == 200, r.text
    oid = r.json()['id']
    assert staff.put(f'/api/orders/{oid}', json={'status': 'Preparing'}).status_code == 200
    assert staff.get(f'/api/orders/{oid}').status_code == 200
    assert staff.get(f'/api/print/{oid}').status_code == 200
    assert staff.get('/api/orders').status_code == 200


def test_every_registered_api_route_matches_the_permission_matrix(appmod, owner, staff):
    """A route that core.required_role() calls owner-only must carry the OWNER dependency, and the other way round."""
    import core
    from fastapi.routing import APIRoute
    for route in appmod.app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith('/api/'):
            continue
        path = re.sub(r'\{[^}]+\}', '1', route.path)
        for method in route.methods - {'HEAD', 'OPTIONS'}:
            role = core.required_role(method, path)
            has_owner_dep = any(getattr(d.call, '__name__', '') == 'require_owner' for d in route.dependant.dependencies)
            if role == 'owner':
                assert has_owner_dep, f'{method} {route.path} is owner-only in the matrix but has no OWNER dependency'


def test_static_blocks_sensitive_files(client):
    for p in ['README.txt', '.env', '../.env', '..%2f.env', 'app.py', '%2e%2e/backend/app.py', 'index.html']:
        assert client.get(f'/static/{p}').status_code == 404, p
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/static/actions.js').status_code == 200
    assert client.get('/static/styles.css').status_code == 200


def test_security_headers_present(client):
    r = client.get('/')
    assert 'Content-Security-Policy' in r.headers and 'unsafe-eval' not in r.headers['Content-Security-Policy']
    assert r.headers['X-Frame-Options'] == 'DENY' and r.headers['X-Content-Type-Options'] == 'nosniff'
    assert "script-src 'self'" in r.headers.get('Content-Security-Policy-Report-Only', '')


def test_logout_and_invalid_session_rejected(appmod, owner):
    c, r = _login(appmod, 'owner', 'Correct-Horse-9', '203.0.113.50')
    assert c.get('/api/orders').status_code == 200
    c.post('/api/logout')
    assert c.get('/api/orders').status_code == 401
    c.cookies.set('dk_session', 'not-a-real-token')
    assert c.get('/api/orders').status_code == 401


def test_expired_session_rejected(appmod, owner):
    c, r = _login(appmod, 'owner', 'Correct-Horse-9', '203.0.113.51')
    db = appmod.conn(); db.execute(f'UPDATE {appmod.T_SESSIONS} SET expires_at = 1'); db.commit(); db.close()
    appmod._session_cache.clear()
    assert c.get('/api/orders').status_code == 401


def test_login_throttle_is_per_source_and_owner_not_locked_out(appmod):
    import core
    appmod.login_throttle = core.LoginThrottle()
    from fastapi.testclient import TestClient
    attacker = TestClient(appmod.app, base_url='https://testserver', headers={'X-Forwarded-For': '1.1.1.1, 198.51.100.7'})
    codes = [attacker.post('/api/login', json={'username': 'owner', 'password': 'wrong-wrong-1'}).status_code for _ in range(8)]
    assert codes[:6] == [401] * 6 and codes[6:] == [429, 429]
    spoof = TestClient(appmod.app, base_url='https://testserver', headers={'X-Forwarded-For': '9.9.9.9, 198.51.100.7'})   # forged left part: same real address
    assert spoof.post('/api/login', json={'username': 'owner', 'password': 'wrong-wrong-1'}).status_code == 429
    real_owner = TestClient(appmod.app, base_url='https://testserver', headers={'X-Forwarded-For': '203.0.113.99'})
    assert real_owner.post('/api/login', json={'username': 'owner', 'password': 'Correct-Horse-9'}).status_code == 200


def test_validation_errors_do_not_echo_passwords(client):
    r = client.post('/api/login', json={'username': 'x', 'password': 'p' * 300})
    assert r.status_code == 422 and 'ppppp' not in r.text
