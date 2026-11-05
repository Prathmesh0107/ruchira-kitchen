import threading, pytest
from datetime import datetime, timedelta


def _order(c, item, **kw):
    body = {'items': [{'menu_item_id': item['id'], 'quantity': 1}], 'order_type': 'Pickup', **kw}
    return c.post('/api/orders', json=body)


def test_order_type_allow_list_on_create_and_update(owner, menu_item):
    for bad in ['<img src=x onerror=alert(1)>', "Pickup'); alert(1);//", 'Dine-in', '']:
        assert _order(owner, menu_item, order_type=bad).status_code == 422
    ok = _order(owner, menu_item, order_type='delivery')
    assert ok.status_code == 200 and ok.json()['order_type'] == 'Delivery'
    oid = ok.json()['id']
    assert owner.put(f'/api/orders/{oid}', json={'order_type': '"><script>alert(1)</script>'}).status_code == 422
    assert owner.put(f'/api/orders/{oid}', json={'order_type': 'Pickup'}).json()['order_type'] == 'Pickup'


def test_negative_and_invalid_delivery_fee_rejected(owner, menu_item):
    for bad in [-1, -0.01, 'abc', 1e999, float('nan'), 100001]:
        try:
            r = _order(owner, menu_item, delivery_fee=bad)
        except ValueError:      # the test client itself refuses to serialise NaN
            continue
        assert r.status_code == 422, bad
    r = _order(owner, menu_item, delivery_fee=0)
    assert r.status_code == 200 and float(r.json()['total']) == float(r.json()['subtotal']) + float(r.json()['tax'])
    oid = r.json()['id']
    assert owner.put(f'/api/orders/{oid}', json={'items': [{'menu_item_id': menu_item['id'], 'quantity': 1}], 'delivery_fee': -50}).status_code == 422


def test_unicode_names_survive(owner):
    r = owner.post('/api/menu-items', json={'name': 'चिकन मंचाव सूप (हाफ)', 'category': 'नॉनव्हेज सूप', 'price': 110, 'available': True})
    assert r.status_code == 200 and r.json()['name'] == 'चिकन मंचाव सूप (हाफ)'


def test_xss_looking_names_are_stored_verbatim_and_returned_as_data(owner):
    payload = '&quot;+alert(1)+&quot;'
    c = owner.post('/api/categories', json={'name': payload, 'icon': '🍕'})
    assert c.status_code == 200 and c.json()['name'] == payload      # the server stores text; the browser must render it as text (frontend tests)


def test_concurrent_orders_get_unique_consecutive_numbers(appmod, owner, menu_item):
    results, errs = [], []
    def go():
        try:
            from fastapi.testclient import TestClient
            c = TestClient(appmod.app, base_url='https://testserver'); c.cookies.update(owner.cookies)
            results.append(_order(c, menu_item).json()['order_number'])
        except Exception as e:
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs and len(results) == 12 and len(set(results)) == 12
    assert max(results) - min(results) == 11


def test_client_supplied_order_number_is_not_trusted(owner, menu_item):
    r = _order(owner, menu_item, order_number=999999)
    assert r.status_code == 200 and r.json()['order_number'] != 999999
    nxt = owner.get('/api/next-order-number').json()['next']
    assert nxt == r.json()['order_number'] + 1


def test_duplicate_submission_returns_the_same_order(owner, menu_item):
    rid = 'req_' + 'a1b2c3d4e5f6'
    a = _order(owner, menu_item, client_request_id=rid).json()
    b = _order(owner, menu_item, client_request_id=rid).json()
    assert a['id'] == b['id'] and a['order_number'] == b['order_number']


def test_unique_order_number_enforced_by_database(appmod, owner, menu_item):
    import psycopg
    o = _order(owner, menu_item).json()
    c = appmod.conn()
    with pytest.raises(psycopg.errors.UniqueViolation):
        c.execute("INSERT INTO orders(order_number,order_type,created_at) VALUES (%s,'Pickup', now())", (o['order_number'],))
    c.rollback(); c.close()


def test_rollback_when_item_insert_fails(appmod, owner, menu_item, monkeypatch):
    before = appmod.conn().execute('SELECT COUNT(*) AS n FROM orders').fetchone()['n']
    def boom(self, *a, **k): raise RuntimeError('simulated item insert failure')
    monkeypatch.setattr(appmod.PooledConn, 'executemany', boom)
    from fastapi.testclient import TestClient
    c = TestClient(appmod.app, base_url='https://testserver', raise_server_exceptions=False); c.cookies.update(owner.cookies)
    r = _order(c, menu_item)
    assert r.status_code == 500 and 'simulated' not in r.text           # generic message, no internals
    after = appmod.conn().execute('SELECT COUNT(*) AS n FROM orders').fetchone()['n']
    assert after == before                                               # no half-created order


def test_pagination_bounds(owner, menu_item):
    for _ in range(3): _order(owner, menu_item)
    assert len(owner.get('/api/orders?limit=2').json()) == 2
    assert owner.get('/api/orders?limit=0').status_code == 422
    assert owner.get('/api/orders?limit=999999').status_code == 422
    assert owner.get('/api/orders?month=2026-13').status_code == 400


def test_reports_use_india_month_boundaries(appmod, owner, menu_item):
    """An order stored at 20:00 UTC on 31 March is 01:30 IST on 1 April: it belongs to APRIL."""
    c = appmod.conn()
    c.execute("INSERT INTO orders(order_number,order_type,total,subtotal,status,created_at) VALUES (990001,'Pickup',100,100,'Completed','2031-03-31 20:00:00')")
    c.execute("INSERT INTO orders(order_number,order_type,total,subtotal,status,created_at) VALUES (990002,'Pickup',200,200,'Completed','2031-04-30 18:29:59')")   # 23:59:59 IST on 30 April
    c.execute("INSERT INTO orders(order_number,order_type,total,subtotal,status,created_at) VALUES (990003,'Pickup',400,400,'Completed','2031-04-30 18:30:00')")   # 00:00 IST on 1 May
    c.commit(); c.close()
    april, may, march = (owner.get(f'/api/reports?month=2031-0{m}').json() for m in (4, 5, 3))
    assert april['revenue'] == 300.0 and april['orders'] == 2
    assert may['revenue'] == 400.0 and march['revenue'] == 0


def test_errors_are_generic(appmod, owner):
    r = owner.post('/api/inventory', json={'item_name': 'x', 'amount': 5, 'purchase_date': '2031-02-30'})
    assert r.status_code == 400 and 'Traceback' not in r.text and 'psycopg' not in r.text


def test_demo_reset_disabled_by_default(owner):
    assert owner.post('/api/demo/reset').status_code == 403
