FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY certs/Russian_Trusted_Root_CA.cer /usr/local/share/ca-certificates/Russian_Trusted_Root_CA.crt
COPY certs/Russian_Trusted_Sub_CA.cer /usr/local/share/ca-certificates/Russian_Trusted_Sub_CA.crt
COPY certs/Russian_Trusted_Sub_CA_2024.cer /usr/local/share/ca-certificates/Russian_Trusted_Sub_CA_2024.crt
RUN update-ca-certificates

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY app ./app
COPY tests ./tests
RUN pytest -q && touch /tmp/tests-passed

FROM base AS runtime
COPY --from=test /tmp/tests-passed /tmp/tests-passed
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY entrypoint.sh ./entrypoint.sh

CMD ["./entrypoint.sh"]
