FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt
RUN pip install \
    "trafilatura>=1.12.2,<2.0" \
    "rapidfuzz>=3.0" \
    "readability-lxml>=0.8.1"

COPY . .

EXPOSE 8077 8078
