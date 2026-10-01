# One image for every LIF control/data-plane service (gateway, controller, decision-fabric,
# batch, console). The Deployment chooses the app: `uvicorn lif.<svc>.app:app`. arm64 (DGX Spark).

# Labzilla Console PWA (apps/console): a static bundle, built here so the runtime image needs no
# Node. package-lock.json pins every package, including the linux-arm64 native binaries
# (rolldown, lightningcss, typescript). Playwright is a dev dependency for e2e only: no browsers.
FROM node:24.16.0-bookworm-slim AS ui
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 NPM_CONFIG_AUDIT=false NPM_CONFIG_FUND=false NPM_CONFIG_UPDATE_NOTIFIER=false
WORKDIR /ui
COPY apps/console/package.json apps/console/package-lock.json ./
RUN npm ci
COPY apps/console ./
RUN npm run build

FROM python:3.12.11-slim-bookworm
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN pip install "fastapi==0.118.0" "uvicorn[standard]==0.37.0" "httpx==0.28.1" "pydantic==2.11.9" \
                "pyyaml==6.0.2" "prometheus-client==0.23.1"
COPY lif ./lif
COPY config ./config
COPY evals ./evals
COPY apps ./apps
COPY --from=ui /ui/dist ./apps/console/dist
COPY decision-packages ./decision-packages
# Public knowledge repos only (data_class PUBLIC), for the console's read-only Knowledge pages.
# The private repo lives outside lif/ (../private/knowledge), so it is never in the build context;
# .dockerignore drops the derived index (knowledge/.knowledge), which may have indexed it.
COPY knowledge ./knowledge
# Run from /app (not site-packages) so lif/ finds its bundled config/ and evals/.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin lif \
    && mkdir -p /data && chown lif /data
USER 10001
EXPOSE 8080
ENTRYPOINT ["uvicorn", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-graceful-shutdown", "25"]
CMD ["lif.gateway.app:app"]
