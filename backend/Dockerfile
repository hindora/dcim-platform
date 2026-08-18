FROM python:3.11-slim

# Non-root: the backend needs no privileged ports and no host access.
RUN useradd --create-home --uid 10001 dcim

WORKDIR /app

# Contracts first: they change less often than application code, so the
# dependency layer stays cached.
COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY contracts /app/contracts

WORKDIR /app/backend
RUN pip install --no-cache-dir -e . && rm -rf /root/.cache

COPY backend /app/backend

USER dcim
EXPOSE 8000

# No CMD: docker-compose supplies it, because the same image runs the API, the
# ingest worker and the importer.
