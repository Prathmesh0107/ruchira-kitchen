from pathlib import Path
import os
from dotenv import load_dotenv
from psycopg import connect
from psycopg.rows import dict_row

root = Path(__file__).resolve().parent.parent
load_dotenv(root / ".env")
url = os.getenv("DATABASE_URL", "").strip()
assert url, "no DATABASE_URL"
kwargs = {"row_factory": dict_row, "connect_timeout": 10}
if "sslmode=" not in url:
    kwargs["sslmode"] = "require"
c = connect(url, **kwargs)
cols = c.execute(
    """
    SELECT column_name, data_type, is_nullable, character_maximum_length
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='settings'
    ORDER BY ordinal_position
    """
).fetchall()
row = c.execute("SELECT * FROM settings WHERE id=1").fetchone()
print("COLUMNS")
for col in cols:
    print(repr(dict(col)))
print("ROW_KEYS", sorted((row or {}).keys()))
print("ROW_TYPES", {k: type(v).__name__ for k, v in (row or {}).items()})
print("BIZ", (row or {}).get("business_name"))
c.close()
