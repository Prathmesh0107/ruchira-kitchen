"""Unit tests for backend/core.py. Standard library only:  python -m unittest discover -s tests -v"""
import sys, unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))
import core  # noqa: E402


class OrderTypeTests(unittest.TestCase):
    def test_accepts_known_types_and_normalises_case(self):
        self.assertEqual(core.normalize_order_type('Pickup'), 'Pickup')
        self.assertEqual(core.normalize_order_type('delivery'), 'Delivery')
        self.assertEqual(core.normalize_order_type('  DELIVERY '), 'Delivery')
        self.assertEqual(core.normalize_order_type('Pickup\n'), 'Pickup')   # whitespace is trimmed; the stored value is always the canonical one

    def test_rejects_everything_else(self):
        for bad in ['', 'Dine-in', '<img src=x onerror=alert(1)>', "Pickup'); alert(1);//", '"><script>', 'Pick up', 'Pickup;Delivery', 'PickupDelivery', None, 5, ['Pickup']]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                core.normalize_order_type(bad)


class MoneyTests(unittest.TestCase):
    def test_valid_amounts(self):
        self.assertEqual(core.parse_money(0), Decimal('0.00'))
        self.assertEqual(core.parse_money('30'), Decimal('30.00'))
        self.assertEqual(core.parse_money(12.345), Decimal('12.35'))   # 12.345 -> half-up on the decimal text
        self.assertEqual(core.parse_money(Decimal('49.999')), Decimal('50.00'))

    def test_rejects_negative_nan_inf_bool_text(self):
        for bad in [-1, -0.01, '-5', float('nan'), float('inf'), float('-inf'), 'NaN', 'Infinity', True, False, None, 'abc', '', '1e999999', 10**9]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                core.parse_money(bad, 'Delivery fee')

    def test_negative_delivery_fee_cannot_reduce_total(self):
        with self.assertRaises(ValueError):
            core.compute_totals([(100, 1)], 0, -50)
        t = core.compute_totals([(100, 2), (49.5, 1)], 5, 30)
        self.assertEqual(t['subtotal'], Decimal('249.50'))
        self.assertEqual(t['tax'], Decimal('12.48'))   # 12.475 rounds half-up
        self.assertEqual(t['total'], Decimal('291.98'))
        self.assertGreaterEqual(t['total'], t['subtotal'])

    def test_zero_fee_orders_still_work(self):
        t = core.compute_totals([(99, 3)], 0, 0)
        self.assertEqual(t['total'], Decimal('297.00'))


class TimeTests(unittest.TestCase):
    def test_ist_midnight_is_previous_day_in_utc(self):
        # 00:30 IST on 1 April 2026 == 19:00 UTC on 31 March
        stored = datetime(2026, 3, 31, 19, 0, 0)   # naive UTC, as the server stores it
        self.assertEqual(core.ist_business_date(stored), date(2026, 4, 1))
        start, end = core.month_bounds_utc('2026-04')
        self.assertTrue(start <= stored < end, 'an order at 00:30 IST on 1 April must be in April')
        march = core.month_bounds_utc('2026-03')
        self.assertFalse(march[0] <= stored < march[1], 'and must NOT be in March')

    def test_month_bounds_values(self):
        start, end = core.month_bounds_utc('2026-04')
        self.assertEqual(start, datetime(2026, 3, 31, 18, 30))
        self.assertEqual(end, datetime(2026, 4, 30, 18, 30))

    def test_last_second_of_month_in_india(self):
        start, end = core.month_bounds_utc('2026-04')
        last = datetime(2026, 4, 30, 18, 29, 59)   # 23:59:59 IST on 30 April
        first_may = datetime(2026, 4, 30, 18, 30, 0)   # 00:00:00 IST on 1 May
        self.assertTrue(start <= last < end)
        self.assertFalse(start <= first_may < end)
        self.assertTrue(core.month_bounds_utc('2026-05')[0] <= first_may < core.month_bounds_utc('2026-05')[1])

    def test_december_rolls_into_next_year_and_february_leap(self):
        s, e = core.month_bounds_utc('2025-12')
        self.assertEqual((s, e), (datetime(2025, 11, 30, 18, 30), datetime(2025, 12, 31, 18, 30)))
        s, e = core.month_bounds_utc('2024-02')
        self.assertEqual(e - s, timedelta(days=29))
        s, e = core.month_bounds_utc('2026-02')
        self.assertEqual(e - s, timedelta(days=28))

    def test_months_tile_the_timeline_without_gaps_or_overlap(self):
        prev_end = None
        for m in range(1, 13):
            s, e = core.month_bounds_utc(f'2026-{m:02d}')
            if prev_end is not None:
                self.assertEqual(s, prev_end)
            prev_end = e

    def test_orders_whose_utc_date_differs_from_business_date(self):
        # 23:45 UTC on 30 June = 05:15 IST on 1 July
        stored = datetime(2026, 6, 30, 23, 45)
        self.assertEqual(stored.date(), date(2026, 6, 30))
        self.assertEqual(core.ist_business_date(stored), date(2026, 7, 1))
        july = core.month_bounds_utc('2026-07')
        self.assertTrue(july[0] <= stored < july[1])
        # 18:29 UTC = 23:59 IST same UTC date: stays in June
        self.assertEqual(core.ist_business_date(datetime(2026, 6, 30, 18, 29)), date(2026, 6, 30))

    def test_day_bounds_around_ist_midnight(self):
        s, e = core.day_bounds_utc(date(2026, 4, 1))
        self.assertEqual((s, e), (datetime(2026, 3, 31, 18, 30), datetime(2026, 4, 1, 18, 30)))
        self.assertTrue(s <= datetime(2026, 3, 31, 18, 30) < e)       # 00:00 IST sharp is in the day
        self.assertFalse(s <= datetime(2026, 3, 31, 18, 29, 59) < e)   # 23:59:59 IST the day before is not

    def test_db_timestamp_requires_aware_and_converts_to_naive_utc(self):
        with self.assertRaises(ValueError):
            core.db_timestamp(datetime(2026, 1, 1))
        self.assertEqual(core.db_timestamp(datetime(2026, 1, 1, 5, 30, tzinfo=core.IST)), datetime(2026, 1, 1, 0, 0))
        self.assertIsNone(core.db_timestamp(datetime.now(core.UTC)).tzinfo)

    def test_month_parsing(self):
        for bad in ['2026-13', '2026-00', '1999-12', '2101-01', 'abc', '2026-1', '2026/04', '2026-04-01']:
            with self.assertRaises(ValueError, msg=bad):
                core.parse_month(bad)
        self.assertRegex(core.current_month_ist(), r'^\d{4}-\d{2}$')


class ClientIpTests(unittest.TestCase):
    def test_one_trusted_proxy_uses_last_entry_not_forged_first(self):
        self.assertEqual(core.client_ip('6.6.6.6, 1.2.3.4', '10.0.0.1', 1), '1.2.3.4')
        self.assertEqual(core.client_ip('6.6.6.6, 7.7.7.7, 1.2.3.4', '10.0.0.1', 1), '1.2.3.4')

    def test_two_trusted_proxies(self):
        self.assertEqual(core.client_ip('6.6.6.6, 1.2.3.4, 10.1.1.1', '10.0.0.1', 2), '1.2.3.4')

    def test_no_proxy_ignores_header_entirely(self):
        self.assertEqual(core.client_ip('1.2.3.4', '9.9.9.9', 0), '9.9.9.9')

    def test_garbage_and_missing_headers_fall_back_to_peer(self):
        for xff in ['', 'garbage', '999.1.1.1', '<script>', ',,,', 'a' * 5000]:
            self.assertEqual(core.client_ip(xff, '9.9.9.9', 1), '9.9.9.9', xff[:20])

    def test_too_few_hops_falls_back(self):
        self.assertEqual(core.client_ip('1.2.3.4', '9.9.9.9', 2), '9.9.9.9')

    def test_ipv6_is_bucketed_per_64(self):
        a = core.client_ip('2001:db8:1:2:aaaa::1', '', 1)
        b = core.client_ip('2001:db8:1:2:bbbb::9', '', 1)
        c = core.client_ip('2001:db8:1:3::1', '', 1)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(core.client_ip('::ffff:1.2.3.4', '', 1), '1.2.3.4')


class FakeClock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.t = core.LoginThrottle(pair_limit=6, ip_limit=30, window=600, clock=self.clock)

    def test_one_source_is_limited_after_six_failures(self):
        for _ in range(6):
            self.assertFalse(self.t.blocked('1.1.1.1', 'owner'))
            self.t.record_failure('1.1.1.1', 'owner')
        self.assertTrue(self.t.blocked('1.1.1.1', 'owner'))

    def test_attacker_cannot_lock_the_owner_out_from_another_address(self):
        for _ in range(500):   # attacker hammers the owner's username from the same few addresses
            for ip in ('6.6.6.6', '6.6.6.7'):
                self.t.record_failure(ip, 'Owner')
        self.assertTrue(self.t.blocked('6.6.6.6', 'owner'))
        self.assertFalse(self.t.blocked('203.0.113.9', 'owner'), 'owner from a different address must still be able to log in')
        self.assertGreater(self.t.username_failures('owner'), 100)   # but it is visible for alerting

    def test_username_is_case_insensitive_for_the_pair_limit(self):
        for u in ['Owner', 'OWNER', 'owner ', ' oWnEr', 'owner', 'Owner']:
            self.t.record_failure('1.1.1.1', u)
        self.assertTrue(self.t.blocked('1.1.1.1', 'owner'))

    def test_per_address_limit_stops_username_spraying(self):
        for i in range(30):
            self.t.record_failure('5.5.5.5', f'user{i}')
        self.assertTrue(self.t.blocked('5.5.5.5', 'brand-new-name'))
        self.assertFalse(self.t.blocked('5.5.5.6', 'brand-new-name'))

    def test_window_expires(self):
        for _ in range(6): self.t.record_failure('1.1.1.1', 'x')
        self.assertTrue(self.t.blocked('1.1.1.1', 'x'))
        self.clock.t += 601
        self.assertFalse(self.t.blocked('1.1.1.1', 'x'))

    def test_success_clears_the_pair(self):
        for _ in range(5): self.t.record_failure('1.1.1.1', 'x')
        self.t.record_success('1.1.1.1', 'x')
        self.assertFalse(self.t.blocked('1.1.1.1', 'x'))

    def test_memory_is_bounded(self):
        t = core.LoginThrottle(max_keys=100, clock=self.clock)
        for i in range(5000):
            self.clock.t += 0.001
            t.record_failure(f'10.0.{i // 250}.{i % 250}', f'u{i}')
        self.assertLessEqual(len(t._pair), 200)

    def test_spoofed_forwarded_headers_do_not_create_fresh_buckets(self):
        # attacker rotates the LEFT part of X-Forwarded-For; the trusted (right-most) entry stays theirs
        for i in range(6):
            ip = core.client_ip(f'1.1.1.{i}, 8.8.8.8', '10.0.0.1', 1)
            self.t.record_failure(ip, 'owner')
        self.assertTrue(self.t.blocked(core.client_ip('9.9.9.9, 8.8.8.8', '10.0.0.1', 1), 'owner'))


class RoleMatrixTests(unittest.TestCase):
    OWNER_ONLY = [
        ('GET', '/api/users'), ('POST', '/api/users'), ('PUT', '/api/users/3'), ('DELETE', '/api/users/3'),
        ('PUT', '/api/settings'), ('POST', '/api/orders/clear'), ('POST', '/api/demo/reset'),
        ('DELETE', '/api/orders/5'), ('GET', '/api/reports'),
        ('GET', '/api/inventory'), ('POST', '/api/inventory'), ('PUT', '/api/inventory/4'), ('DELETE', '/api/inventory/4'), ('GET', '/api/inventory/items'),
        ('POST', '/api/categories'), ('PUT', '/api/categories/2'), ('DELETE', '/api/categories/2'), ('POST', '/api/categories/reorder'),
        ('POST', '/api/menu-items'), ('DELETE', '/api/menu-items/9'), ('POST', '/api/printers/test'),
        ('GET', '/docs'), ('GET', '/openapi.json'), ('GET', '/api/something-new'), ('PATCH', '/api/orders/5'),
        ('POST', '/api/tables'), ('DELETE', '/api/tables/3'), ('PUT', '/api/tables/3'), ('POST', '/api/tables/x/free'),
    ]
    STAFF_OK = [
        ('GET', '/api/me'), ('POST', '/api/me/password'), ('POST', '/api/logout'), ('GET', '/api/settings'),
        ('GET', '/api/categories'), ('GET', '/api/menu-items'), ('PUT', '/api/menu-items/9'), ('GET', '/api/next-order-number'),
        ('GET', '/api/orders'), ('POST', '/api/orders'), ('GET', '/api/orders/5'), ('PUT', '/api/orders/5'),
        ('GET', '/api/print/5'), ('POST', '/api/printers/print'), ('POST', '/api/printers/print-batch'), ('GET', '/api/printers'),
        ('GET', '/api/tables'), ('POST', '/api/tables/3/free'),
    ]

    def test_owner_only_routes(self):
        for m, p in self.OWNER_ONLY:
            self.assertEqual(core.required_role(m, p), 'owner', f'{m} {p}')

    def test_staff_routes(self):
        for m, p in self.STAFF_OK:
            self.assertEqual(core.required_role(m, p), 'staff', f'{m} {p}')

    def test_login_is_public(self):
        self.assertEqual(core.required_role('POST', '/api/login'), 'public')

    def test_path_tricks_do_not_grant_staff_access(self):
        for m, p in [('GET', '/api/users/'), ('GET', '//api//users'), ('GET', '/api/orders/5/../../users'), ('DELETE', '/api/orders/5/'),
                     ('DELETE', '/api/orders/abc'), ('GET', '/api/orders/5; DROP'), ('GET', '/api/menu-items/9'), ('POST', '/api/orders/clear/'),
                     ('GET', '/api/reports/'), ('GET', '/api/orders/99999999999999999999')]:
            self.assertEqual(core.required_role(m, p), 'owner', f'{m} {p}')

    def test_every_staff_rule_is_get_post_or_put_only(self):
        for methods, _ in core.STAFF_ALLOWED:
            self.assertFalse(methods & {'DELETE', 'PATCH'})


class PasswordPolicyTests(unittest.TestCase):
    def test_rejections(self):
        for bad in ['', 'short', 'admin123', 'Admin123', 'password123', '1234567890', 'aaaaaaaaaaaa', 'abababababab'[:11], 'x' * 129]:
            self.assertIsNotNone(core.password_problem(bad, 'owner'), repr(bad))
        self.assertIsNotNone(core.password_problem('rameshkumar', 'RameshKumar'))

    def test_acceptances_including_unicode(self):
        for good in ['correct horse battery', 'Ruchira#2026-kitchen', 'चहा-आणि-वडापाव-९९', 'x7Kp-2mQz-91']:
            self.assertIsNone(core.password_problem(good, 'owner'), good)

    def test_known_default_is_flagged(self):
        self.assertIn('admin123', core.KNOWN_DEFAULT_PASSWORDS)


class StaticAndHeaderTests(unittest.TestCase):
    def test_assets_allowed(self):
        for ok in ['app.js', 'actions.js', 'bluetooth.js', 'styles.css', 'logo.png', 'img/logo.svg', 'fonts/a-b.woff2']:
            self.assertTrue(core.static_path_allowed(ok), ok)

    def test_sensitive_files_blocked(self):
        for bad in ['README.txt', 'readme.md', '.env', '.env.example', '../.env', 'a/../../.env', '..%2f.env', '%2e%2e/x.js', 'app.js.map', 'app.js.bak', 'app.py',
                    'requirements.txt', 'index.html', '.git/config', 'sub/.hidden.js', '/etc/passwd', 'a\\b.js', 'x.js\x00.txt', '', 'noext', 'dir/', 'a//b.js', 'run.py', 'x.JS.txt']:
            self.assertFalse(core.static_path_allowed(bad), repr(bad))

    def test_headers(self):
        h = core.security_headers(https=True, csp_mode='report-only')
        self.assertIn("script-src 'self' 'unsafe-inline'", h['Content-Security-Policy'])
        self.assertIn("script-src 'self'", h['Content-Security-Policy-Report-Only'])
        self.assertNotIn('unsafe-inline', h['Content-Security-Policy-Report-Only'].split('style-src')[0])
        self.assertNotIn('unsafe-eval', ''.join(h.values()))
        self.assertIn('Strict-Transport-Security', h)
        strict = core.security_headers(https=False, csp_mode='enforce')
        self.assertEqual(strict['Content-Security-Policy'].split(';')[1].strip(), "script-src 'self'")
        self.assertNotIn('Strict-Transport-Security', strict)
        self.assertNotIn('Content-Security-Policy', core.security_headers(https=False, csp_mode='off'))
        for key in ("object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'", "connect-src 'self'"):
            self.assertIn(key, strict['Content-Security-Policy'])


if __name__ == '__main__':
    unittest.main()
