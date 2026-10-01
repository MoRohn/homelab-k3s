# One image for every LIF control/data-plane service (gateway, controller, decision-fabric,
# batch). The Deployment chooses the app: `uvicorn lif.<svc>.app:app`. arm64 (DGX Spark).
FROM python:3.12.11-slim-bookworm
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN pip install "fastapi==0.118.0" "uvicorn[standard]==0.37.0" "httpx==0.28.1" "pydantic==2.11.9" \
                "pyyaml==6.0.2" "prometheus-client==0.23.1"
COPY lif ./lif
COPY config ./config
COPY evals ./evals
COPY apps ./apps
# Run from /app (not site-packages) so lif/ finds its bundled config/ and evals/.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin lif \
    && mkdir -p /data && chown lif /data
USER 10001
EXPOSE 8080
ENTRYPOINT ["uvicorn", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-graceful-shutdown", "25"]
CMD ["lif.gateway.app:app"]
