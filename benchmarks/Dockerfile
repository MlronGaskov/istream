FROM python:3.12-slim
WORKDIR /app
COPY clients/test_client/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt 'pytest>=8,<10'
COPY implementations/istream/istream /app/istream
COPY clients/test_client/*.py /app/clients/
COPY db/migrations /app/migrations
COPY benchmarks /app/benchmarks
ENV PYTHONPATH=/app:/app/clients
CMD ["python", "-m", "benchmarks.run"]
