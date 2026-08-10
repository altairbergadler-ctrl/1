FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

WORKDIR /sidecar
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY app.py .
COPY test_app.py .

RUN useradd --system --uid 10001 --create-home sidecar
USER sidecar

CMD ["python", "app.py"]
