# RCA assistant service — Project 4 image.  Build:  podman build -t rca-service:0.4 .
# Red Hat UBI (Universal Base Image): freely redistributable, same userland as RHEL, what a bank's platform team expects.
FROM registry.access.redhat.com/ubi9/python-311:latest

# UBI python images run as uid 1001 with WORKDIR /opt/app-root/src — no root in the container.
COPY --chown=1001:0 service/requirements.txt ./service/
RUN pip install --no-cache-dir -r service/requirements.txt

COPY --chown=1001:0 project1-rca-assistant/ ./project1-rca-assistant/
COPY --chown=1001:0 service/               ./service/
COPY --chown=1001:0 sample_corpus/         ./sample_corpus/
COPY --chown=1001:0 data/alerts.json       ./data/alerts.json
RUN mkdir -p data && chmod g+w data          # traces.jsonl is written here (ephemeral; the durable copy is OpenSearch)

# everything that differs per environment is an env var, not a file edit
ENV OLLAMA_URL=http://localhost:11434 \
    OPENSEARCH_URL=http://localhost:9200 \
    RCA_TRACE_EXPORTER=none \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8080"]
