FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY otel_setup.py app.py ./

ENV PORT=8080
EXPOSE 8080

# Pure Python entrypoint — NO opentelemetry-instrument wrapper, NO ADOT
# Collector sidecar. The app's own otel_setup.py (vanilla OTel SDK plus a
# ~30-line SigV4 signer for the CloudWatch OTLP traces endpoint) wires up
# the tracer provider, exporter, propagators, and auto-instrumentors in-process.
CMD ["python", "app.py"]
