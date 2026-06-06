# syntax=docker/dockerfile:1.7

FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN pip install --upgrade pip setuptools wheel

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]