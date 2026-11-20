FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRINTROOM_DB=/app/data/printroom.db \
    PRINTROOM_HOST=0.0.0.0 \
    PRINTROOM_PORT=8000

WORKDIR /app

# 仅依赖标准库，无需 pip install。
COPY printroom ./printroom

RUN mkdir -p /app/data && \
    useradd --system --uid 10001 --home /app printroom && \
    chown -R printroom:printroom /app

USER printroom

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

CMD ["python3", "-m", "printroom"]
