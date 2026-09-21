FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY certs/Russian_Trusted_Root_CA.cer \
    /usr/local/share/ca-certificates/Russian_Trusted_Root_CA.crt

COPY certs/Russian_Trusted_Sub_CA.cer \
    /usr/local/share/ca-certificates/Russian_Trusted_Sub_CA.crt

COPY certs/Russian_Trusted_Sub_CA_2024.cer \
    /usr/local/share/ca-certificates/Russian_Trusted_Sub_CA_2024.crt

RUN update-ca-certificates

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

CMD ["python", "-m", "app.main"]