Demo Kitchen v26 — security hardening + clearer login errors

IF THE LOGIN SCREEN SAYS "Not Found" (or "does not have the login system")
  The server is still running the OLD backend. Put the new backend/app.py on the server and redeploy.
  Quick check: open  https://YOUR-APP.onrender.com/api/me  in a browser.
    {"detail":"Please log in."}   = new backend is live (good)
    {"detail":"Not Found"}        = old backend is still running -> upload backend/app.py and redeploy
  Environment variables are NOT needed for admin / admin123 to work.

FIRST LOGIN: username admin, password admin123 (change it right away: Main Menu > Change password).
  ADMIN_USERNAME / ADMIN_PASSWORD (optional) only matter the very first time the server starts with an
  empty users table. If the app already started once, change the password inside the app instead.

SECURITY CHANGES IN THIS VERSION
- Fixed: a staff login could plant text that ran as code in the owner's browser (names of items /
  categories). Click handlers now escape & < > ' correctly.
- API documentation (/docs, /redoc, /openapi.json) is switched off. ENABLE_DOCS=true turns it on
  (owner login required).
- No cross-site (CORS) access by default. CORS_ORIGINS=https://site1,https://site2 allows listed sites only.
- API responses are never cached; pages send X-Frame-Options DENY, nosniff, Referrer-Policy, HSTS.
- Login lockout cannot be dodged by faking the X-Forwarded-For header: 6 wrong tries per address or
  12 per username = locked for 10 minutes.
- Server errors no longer return internal details (for example database connection text).
- Only the owner can choose a printer / port in print requests; staff always use the saved printer.
- Fixed: Windows printer "Use" buttons did not work for printer names containing quotes.

----------------------------------------------------------------------

Demo Kitchen v25 — login + passwords, simple inventory, newest-on-top, bigger order picker

WHERE EACH FILE GOES
- frontend/: app.js, bluetooth.js, styles.css, index.html
- backend/:  app.py        (backend CHANGED: redeploy on Render)

WHAT'S NEW
1. Kitchen dashboard: newest order is on TOP in every column, older ones below it.
2. Inventory: big "+ Add Purchase" on the left, month on the right. "Quick stock" buttons for every
   item (grouped by type: Dairy, Vegetables...). Tap Paneer, type the amount (e.g. 2000), Save.
   Buy it again later = tap it again. "Where the money goes" ranks items by spend (this month / all time).
3. New Order: bigger category buttons, bigger item buttons, orange "<- Back to Categories" button that
   stays visible while you scroll.
4. Login: everyone signs in with a username + password. Anyone can change their own password any time
   (Main Menu > Change password). Owners manage logins in Settings > Logins (add, reset password,
   switch off, remove). Owner = everything. Staff = everything except Settings, Logins, Clear all orders.

FIRST LOGIN
- Username: admin    Password: admin123   (the app shows a warning until it is changed)
- Optional Render env vars BEFORE first start: ADMIN_USERNAME and ADMIN_PASSWORD set your own first owner.
- Emergency switch: set AUTH_REQUIRED=false on Render to turn the login off (then set it back).
- Wrong password 6 times = locked for 10 minutes. Sessions last 30 days. Changing your password signs
  you out on your other devices.

TABLES
- users and sessions tables are created automatically on startup. If your database blocks that, run once
  in the Supabase SQL editor:
    CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'staff', active BOOLEAN NOT NULL DEFAULT TRUE, must_change_password BOOLEAN NOT NULL DEFAULT FALSE, created_at TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL);

ALSO FIXED
- /static can no longer be used to download files outside frontend/ (such as .env).
- Settings insert on a brand-new database had one placeholder too many.

----------------------------------------------------------------------

Demo Kitchen v23 — main menu, new kitchen dashboard, delete, today's total, optional name/phone, food license

WHERE EACH FILE GOES
- frontend/: app.js, bluetooth.js, styles.css, index.html
- backend/: app.py            (backend CHANGED this time, so redeploy on Render)

WHAT'S NEW
1. Main Menu is the first page: Dashboard, Orders, Menu, Inventory, Reports, Printer Setup, Settings.
   Every other page has a "<- Main Menu" button. The old sidebar / bottom bar is gone.
2. Kitchen Dashboard: big green "+ New Order" top-left, Month picker top-right, today's numbers,
   4 simple columns (New / Cooking / Ready / Done) with one big next-step button per order.
   Delete button on every order (asks first). Customer name and phone are now OPTIONAL.
3. Orders page: "Print Today's Total" = one slip with every order of today, paid/pending split and the
   grand total (cancelled orders are not counted). "Print each ticket" is the old print-all.
4. Settings > "Clear all orders" deletes every order (type RESET to confirm). Menu, inventory and
   settings are kept. Demo orders are no longer re-created on restart (see SEED_DEMO_ORDERS below).
5. Settings > "Food license (FSSAI) number" is printed under the business name on every receipt
   (Bluetooth, browser, Windows/COM) and on Today's Total. Empty = not printed.

DEPLOY NOTES
- Upload the 4 frontend files + backend/app.py, then redeploy on Render.
- The food_license column is added to the settings table automatically on startup. If you prefer to
  do it by hand, run once in the Supabase SQL editor:
      ALTER TABLE settings ADD COLUMN IF NOT EXISTS food_license TEXT NOT NULL DEFAULT '';
- To get to zero orders right now (without waiting for the deploy), run in the Supabase SQL editor:
      DELETE FROM order_items;
      DELETE FROM orders;
- Render env: leave SEED_DEMO_DATA as is. Demo ORDERS are only created if you set SEED_DEMO_ORDERS=true.
- Hard-refresh the phone browser once (assets are versioned ?v=23).

----------------------------------------------------------------------

Demo Kitchen v21 — clear steps on iPhone / browsers without Bluetooth

- On iPhone/iPad Safari or Chrome, Web Bluetooth is blocked by Apple. Printer Setup now shows exactly
  what to do (install Bluefy, copy the app link, open it there) plus a "Copy app link" button,
  and hides the greyed-out printer controls that only caused confusion.
- Android / laptop Chrome and Edge need nothing extra.

----------------------------------------------------------------------

Demo Kitchen v20 — Print never opens the browser print page when Bluetooth is available

- Print now uses Bluetooth by default on any browser that supports it. The first Print asks you to
  pick the printer once (small popup), then every Print goes straight to the printer.
- Print uses the order already on screen, so there is no server wait before printing starts.
- Top-bar printer chip shows "Tap to connect" in amber until a printer is connected; tap it to
  connect or reconnect from any screen.
- Printer Setup > "Use browser printing instead" switches a device back to the normal print page.
- A configured Windows / COM printer keeps working exactly as before.
- After reloading the page, tap the printer chip once to reconnect (browsers require a tap).

----------------------------------------------------------------------

Demo Kitchen v19 — Bluetooth printing from the phone (Web Bluetooth)

New file:
- frontend/bluetooth.js  (loaded before app.js)

Changed files:
- frontend/app.js, frontend/styles.css, frontend/index.html (backend is unchanged)

What it does:
- Printer Setup now has a "Phone / Bluetooth" tab: Connect printer, Print test receipt, Disconnect.
- Once connected, every Print / Print all button sends a 58mm ESC/POS receipt straight to the printer.
- The printer chip in the top bar shows the Bluetooth printer; tap it to reconnect.
- Works in Chrome / Edge (Android, Windows, Mac) over https:// or localhost. Not iPhone Safari/Chrome (use Bluefy).
- Only Bluetooth Low Energy printers appear. The choice is remembered per browser, not on the server.
- Windows / COM printing and browser print still work exactly as before.

----------------------------------------------------------------------

Demo Kitchen v16 — fast order confirmation / iOS fixes

Files:
- frontend/app.js
- frontend/styles.css
- frontend/index.html
- backend/app.py
- backend/requirements.txt
- run.py

Changes:
- Confirm Order no longer calls the six-resource load() before returning to dashboard.
- No success alert after order creation.
- Newly created order is inserted into local dashboard state immediately after POST succeeds.
- Orders refresh runs in the background and never blocks dashboard rendering.
- Backend create_order returns order items using the existing DB connection; it no longer opens a second connection to serialize the new order.
- Print is separate from order creation.
- iOS mobile form controls use 16px font to prevent Safari focus zoom.
- v14 mobile modal fixes remain included.
- Asset URLs in index.html are cache-busted with ?v=16.
