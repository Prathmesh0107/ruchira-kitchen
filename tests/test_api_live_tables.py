"""Live table squares: /api/tables (needs the same disposable PostgreSQL database as the other test_api_* files)."""

def _order(c, item, table, **kw):
    body = {'items': [{'menu_item_id': item['id'], 'quantity': 1}], 'order_type': 'Pickup', 'table_number': table, **kw}
    r = c.post('/api/orders', json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _table(c, label):
    return next((t for t in c.get('/api/tables').json() if t['label'] == label), None)


def test_starter_tables_exist_and_are_free(owner):
    rows = owner.get('/api/tables').json()
    labels = [t['label'] for t in rows]
    assert labels[:6] == ['1', '2', '3', '4', '5', '6']
    assert all('occupied' in t and 'orders' in t for t in rows)


def test_add_table_automatic_number_and_custom_name(owner):
    before = owner.get('/api/tables').json()
    nums = [int(t['label']) for t in before if t['label'].isdigit()]
    r = owner.post('/api/tables', json={})
    assert r.status_code == 200 and r.json()['label'] == str(max(nums) + 1)
    r2 = owner.post('/api/tables', json={'label': '  Patio   1 '})
    assert r2.status_code == 200 and r2.json()['label'] == 'Patio 1'
    assert owner.post('/api/tables', json={'label': 'patio 1'}).status_code == 409     # same table, other case
    assert owner.post('/api/tables', json={'label': 'x' * 21}).status_code == 422      # too long
    labels = [t['label'] for t in owner.get('/api/tables').json()]
    assert labels.index('Patio 1') > labels.index(r.json()['label'])                    # new tables go to the end


def test_staff_can_view_and_free_but_not_add_or_remove(owner, staff):
    assert staff.get('/api/tables').status_code == 200
    t = owner.post('/api/tables', json={'label': 'Staff-T'}).json()
    assert staff.post('/api/tables', json={'label': 'Nope'}).status_code == 403
    assert staff.delete(f"/api/tables/{t['id']}").status_code == 403
    assert staff.post(f"/api/tables/{t['id']}/free").status_code == 200
    assert owner.delete(f"/api/tables/{t['id']}").status_code == 200


def test_order_makes_table_busy_until_freed(owner, menu_item):
    t = owner.post('/api/tables', json={'label': 'B1'}).json()
    assert _table(owner, 'B1')['occupied'] is False
    o1 = _order(owner, menu_item, ' b1 ')                 # typed with spaces and another case: still table B1
    o2 = _order(owner, menu_item, 'B1')
    row = _table(owner, 'B1')
    assert row['occupied'] and row['order_count'] == 2
    assert [o['id'] for o in row['orders']] == [o1['id'], o2['id']]
    assert abs(row['total'] - (float(o1['total']) + float(o2['total']))) < 0.01
    assert row['since_seconds'] is not None and row['since_seconds'] >= 0
    assert owner.delete(f"/api/tables/{t['id']}").status_code == 409                   # cannot remove a table with guests
    assert owner.post(f"/api/tables/{t['id']}/free").status_code == 200
    assert _table(owner, 'B1')['occupied'] is False
    _order(owner, menu_item, 'B1')
    assert _table(owner, 'B1')['occupied'] is True       # a new sitting starts
    assert owner.post(f"/api/tables/{t['id']}/free").status_code == 200
    assert owner.delete(f"/api/tables/{t['id']}").status_code == 200
    assert _table(owner, 'B1') is None


def test_cancelled_and_other_table_orders_do_not_count(owner, menu_item):
    t = owner.post('/api/tables', json={'label': 'C1'}).json()
    o = _order(owner, menu_item, 'C1')
    _order(owner, menu_item, 'C2-not-a-table')
    assert _table(owner, 'C1')['order_count'] == 1
    assert owner.put(f"/api/orders/{o['id']}", json={'status': 'Cancelled'}).status_code == 200
    assert _table(owner, 'C1')['occupied'] is False
    assert owner.delete(f"/api/tables/{t['id']}").status_code == 200


def test_unknown_table_404(owner):
    assert owner.post('/api/tables/999999/free').status_code == 404
    assert owner.delete('/api/tables/999999').status_code == 404
