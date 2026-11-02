FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
COPY pyproject.toml ./
COPY src ./src
RUN pip install ".[server]" && playwright install --with-deps chromium
COPY . .
CMD ["ci", "run", "--config", "config/targets.yaml"]
