FROM python:3.12-slim

# Utilizador sem privilégios; o PVC deve ser montado com fsGroup 1000 (ver k8s/deployment.yaml).
RUN useradd --uid 1000 --create-home cafe

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

ENV CAFE_DB=/data/cafe.db \
    CAFE_TZ=Europe/Lisbon \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown cafe:cafe /data
VOLUME /data

USER cafe
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
