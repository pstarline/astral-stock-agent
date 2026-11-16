# Resolve and record the resulting image ID on the private host before enabling.
FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Narrow build context: no private files, test fixtures or repository metadata.
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir --disable-pip-version-check .
USER 10001:10001
CMD ["python", "-m", "astral_stock_agent.paper_worker"]
