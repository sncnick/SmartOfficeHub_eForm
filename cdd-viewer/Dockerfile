FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CDD_FORMS_DIR=/app/forms \
    CDD_OUTPUT_DIR=/app/output

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        poppler-utils \
        fontconfig \
        fonts-noto-cjk \
        fonts-arphic-uming \
        fonts-arphic-ukai \
        fonts-liberation \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/*

COPY fonts/99-cdd-cjk.conf /etc/fonts/conf.d/99-cdd-cjk.conf
RUN fc-cache -f

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/output /app/forms \
    && chown appuser /app/output /app/forms
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
