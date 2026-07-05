# Sentinel dashboard — hosted (Cloud Run) image.
# Display-only: serves the committed demo bundle + cached benchmark artifacts.
# No GPU / NIM / GCP client libs, and NO secrets baked in (.env is ignored).
FROM python:3.11-slim

WORKDIR /app

# Deps first for layer caching
COPY dashboard/requirements-app.txt /app/dashboard/requirements-app.txt
RUN pip install --no-cache-dir -r /app/dashboard/requirements-app.txt

# App code + committed demo bundle, and the cached benchmark chart/table
COPY dashboard/ /app/dashboard/
COPY benchmarks/cpu_vs_gpu.png benchmarks/results.csv /app/benchmarks/

# Demo mode: use the committed bundle, hide GPU-only controls
ENV SENTINEL_DEMO=1
ENV PORT=8080
EXPOSE 8080

# Cloud Run sets $PORT; shell form so it expands. Disable CORS/XSRF because
# Cloud Run terminates TLS and proxies to a single port (websockets need this).
CMD streamlit run dashboard/app.py \
    --server.address=0.0.0.0 \
    --server.port=${PORT:-8080} \
    --server.headless=true \
    --server.enableCORS=false \
    --server.enableXsrfProtection=false \
    --browser.gatherUsageStats=false
