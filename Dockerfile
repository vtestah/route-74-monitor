FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
RUN pip install --no-cache-dir -e .

COPY src /app/src
COPY deploy /app/deploy

EXPOSE 8074

CMD ["route74-web", "--host", "0.0.0.0", "--port", "8074"]
