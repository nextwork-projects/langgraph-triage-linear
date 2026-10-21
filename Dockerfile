# The webhook receiver on Cloud Run. Linear allows 5 seconds per delivery, so the image stays slim.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD exec uvicorn receiver:api --host 0.0.0.0 --port ${PORT:-8080}
