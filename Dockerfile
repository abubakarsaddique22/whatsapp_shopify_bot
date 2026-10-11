# One image runs the API, the worker and the jobs; only the command differs.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install uv
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev
COPY src ./src
COPY prompts ./prompts
COPY config ./config
RUN useradd -m app
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src"
HEALTHCHECK CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
