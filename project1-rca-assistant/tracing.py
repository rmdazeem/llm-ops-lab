# -*- coding: utf-8 -*-
"""
OpenTelemetry setup for the RCA assistant.

  RCA_TRACE_EXPORTER = console       one readable line per span (default — for learning)
                     = console-json  full span JSON, exactly what a collector would receive
                     = otlp          send to an OpenTelemetry Collector over OTLP/HTTP
                                     (endpoint from OTEL_EXPORTER_OTLP_ENDPOINT, default http://localhost:4318)
                     = none          tracing off
Every exporter ALSO appends each span to data/traces.jsonl — the eval harness reads that file later.
"""
import os, json, io, sys
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult, SimpleSpanProcessor, BatchSpanProcessor, ConsoleSpanExporter

TRACE_FILE = Path(__file__).resolve().parents[1] / "data" / "traces.jsonl"


def _span_dict(span):
    """Flatten a finished span into plain JSON (what we store for evals)."""
    ctx = span.get_span_context()
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
        "parent_id": format(span.parent.span_id, "016x") if span.parent else None,
        "name": span.name,
        "start": span.start_time, "end": span.end_time,
        "duration_ms": round((span.end_time - span.start_time) / 1e6, 1),
        "status": span.status.status_code.name,
        "attributes": dict(span.attributes),
    }


class CompactConsoleExporter(SpanExporter):
    """One line per span: indent by depth, show duration + the attributes that matter."""
    SHOW = ("gen_ai.request.model", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.tokens_per_s",
            "rca.valid_json", "rca.confidence", "rca.alerts_matched", "rca.files", "rca.scores", "rca.question")

    def export(self, spans):
        for s in spans:
            d = _span_dict(s)
            attrs = {k: v for k, v in d["attributes"].items() if k in self.SHOW}
            depth = 0 if d["parent_id"] is None else 1
            print(f"[trace {d['trace_id'][:8]}] {'  ' * depth}{d['name']:<20} {d['duration_ms']:>9.0f} ms  {attrs}", file=sys.stderr)
        return SpanExportResult.SUCCESS


class JsonlFileExporter(SpanExporter):
    def export(self, spans):
        TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with io.open(TRACE_FILE, "a", encoding="utf-8") as f:
            for s in spans:
                f.write(json.dumps(_span_dict(s)) + "\n")
        return SpanExportResult.SUCCESS


def init(service_name="rca-assistant"):
    mode = os.environ.get("RCA_TRACE_EXPORTER", "console").lower()
    provider = TracerProvider(resource=Resource.create({
        "service.name": service_name,
        "service.version": "0.2",
        "deployment.environment": "laptop-lab",
    }))
    if mode == "console":
        provider.add_span_processor(SimpleSpanProcessor(CompactConsoleExporter()))
    elif mode == "console-json":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
    elif mode == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        print(f"[tracing] OTLP -> {endpoint}", file=sys.stderr)
    if mode != "none":
        provider.add_span_processor(SimpleSpanProcessor(JsonlFileExporter()))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)
