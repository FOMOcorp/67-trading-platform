FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY collectors/requirements.txt /app/collectors/requirements.txt
RUN pip install --no-cache-dir -r /app/collectors/requirements.txt
COPY collectors /app/collectors
COPY clickhouse/init /app/clickhouse/init
COPY parser.py candles.py history.py /app/
CMD ["python", "parser.py", "--storage", "clickhouse"]
