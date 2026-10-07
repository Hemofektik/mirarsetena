FROM node:24-slim AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY mirarsetena ./mirarsetena
COPY config ./config
COPY data ./data
COPY --from=frontend /fe/dist ./frontend/dist

EXPOSE 8000

CMD ["uvicorn", "mirarsetena.app:app", "--host", "0.0.0.0", "--port", "8000"]
