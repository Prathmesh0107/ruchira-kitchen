# Ruchira POS: PostgreSQL / Supabase / Render quick start

The full, current guide is **docs/DEPLOYMENT.md**. Short version:

1. Apply the SQL in `migrations/` yourself (the app no longer creates or alters tables).
2. Set `DATABASE_URL`, and on a brand-new database also `ADMIN_PASSWORD` (there is no default login).
3. Install and run:

```bash
pip install -r backend/requirements.txt      # includes psycopg[binary,pool]
python run.py                                 # local: uvicorn on 127.0.0.1:8000
```

Render: build `pip install -r backend/requirements.txt`, start `uvicorn backend.app:app --host 0.0.0.0 --port $PORT`.
Demo data is OFF by default and is ignored when `APP_ENV=production`.
