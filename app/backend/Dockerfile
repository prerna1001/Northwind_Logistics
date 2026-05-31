FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils curl \
    && rm -rf /var/lib/apt/lists/*

COPY Project/app/backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY Project /app/Project
COPY policies /app/policies
COPY submissions /app/submissions

WORKDIR /app/Project

CMD ["uvicorn", "app.backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/app/Project"]
