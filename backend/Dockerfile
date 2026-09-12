FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY app app
RUN pip install --no-cache-dir .
COPY alembic.ini .
COPY alembic alembic
# The provider domain migrates independently, with its own config and lineage.
COPY alembic_provider.ini .
COPY alembic_provider alembic_provider
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
