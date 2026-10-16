FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt && useradd -r -u 10001 appuser
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini startup.sh ./
USER appuser
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["bash", "startup.sh"]
