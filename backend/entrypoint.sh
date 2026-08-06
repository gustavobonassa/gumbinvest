#!/usr/bin/env bash
# Container entrypoint: wait for Postgres, apply migrations, then exec the CMD.
set -euo pipefail

python - <<'PY'
import os, time, sys
import sqlalchemy as sa

url = os.environ.get("DATABASE_URL", "postgresql+psycopg2://gumbinvest:gumbinvest@db:5432/gumbinvest")
for attempt in range(60):
    try:
        sa.create_engine(url, pool_pre_ping=True).connect().close()
        print("[entrypoint] database is ready")
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        print(f"[entrypoint] waiting for database ({attempt + 1}/60): {exc.__class__.__name__}")
        time.sleep(2)
print("[entrypoint] database unreachable", file=sys.stderr)
sys.exit(1)
PY

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "[entrypoint] applying migrations"
  alembic upgrade head
fi

exec "$@"
