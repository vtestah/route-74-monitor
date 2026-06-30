FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy pyproject.toml and README.md (required by setuptools during package install)
COPY pyproject.toml README.md /app/

# Copy src directory (required by setuptools package finder)
COPY src /app/src

# Install package
RUN pip install --no-cache-dir -e .

# Copy deploy directory
COPY deploy /app/deploy

EXPOSE 8074

CMD ["route74-web", "--host", "0.0.0.0", "--port", "8074"]
