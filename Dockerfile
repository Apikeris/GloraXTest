FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["sh", "-c", "exec gunicorn app:app --config scripts/gunicorn_conf.py --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 2 --timeout 60 --graceful-timeout 10 --access-logfile - --error-logfile -"]
