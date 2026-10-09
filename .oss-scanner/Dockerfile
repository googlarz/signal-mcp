FROM python:3.12-slim

RUN pip install --no-cache-dir uv
WORKDIR /app
COPY . /app
RUN uv sync --frozen
# Tests mock the signal-cli daemon, so no Java or signal-cli is needed.
RUN uv run pytest -q
