FROM python:3.12-slim

WORKDIR /app

COPY clients/test_client/requirements.txt /tmp/client-requirements.txt
COPY workloads/generator/requirements.txt /tmp/generator-requirements.txt

RUN pip install --no-cache-dir -r /tmp/client-requirements.txt -r /tmp/generator-requirements.txt "pytest>=8,<10"

COPY clients/test_client/*.py /app/test_client/
COPY workloads/generator/main.py /app/generator.py
COPY tests/integration/ /app/tests/integration/

ENV PYTHONPATH=/app/test_client

CMD ["pytest", "-q", "tests/integration"]
