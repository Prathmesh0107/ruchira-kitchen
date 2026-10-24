# Demo Kitchen — Supabase/PostgreSQL setup

The app now uses Supabase PostgreSQL through `DATABASE_URL`. The existing HTML/CSS/JS interface and current FastAPI routes remain in place.

## 1. Supabase

The schema you already created in Supabase SQL Editor is sufficient. No second schema script is required for this migration.

## 2. Local setup

Copy `.env.example` to `.env` and paste your Supabase **Session Pooler** connection string into `DATABASE_URL`.

```env
DATABASE_URL=postgresql://...
SEED_DEMO_DATA=true
```

Install:

```bash
pip install -r backend/requirements.txt
```

Run:

```bash
python run.py
```

## 3. Demo vs shop data

For your demo database:

```env
SEED_DEMO_DATA=true
```

This loads sample menu items, orders and inventory only when those tables are empty.

For the real restaurant database:

```env
SEED_DEMO_DATA=false
```

This prevents the Demo Reset action from wiping real data.

## 4. Render

Set these environment variables in Render. Do not commit `.env` to GitHub.

```text
DATABASE_URL=<Supabase Session Pooler URL>
SEED_DEMO_DATA=false
```

The app then uses Supabase instead of the local SQLite file.
