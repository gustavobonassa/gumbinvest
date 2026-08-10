FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# postgresql-client ships pg_dump, used by the automatic backup task
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium for the automated collectors (app/pipelines), via Patchright's CLI.
# --with-deps pulls the shared libraries a browser needs on slim. Note the B3
# collector needs a headed real Chrome to pass Cloudflare, which a headless
# server cannot provide — this keeps the image self-contained for any future
# pipeline that does not, and for local debugging.
RUN patchright install --with-deps chromium

COPY . .

RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
