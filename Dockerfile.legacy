FROM python:3.12-slim

WORKDIR /app

# requirements.lock is generated from uv.lock and pins every runtime wheel by
# version and SHA-256.  The service runs directly from the copied source tree,
# so the image never performs a second, range-resolving project installation.
COPY requirements.lock README.md LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY app ./app
COPY migrations ./migrations

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
