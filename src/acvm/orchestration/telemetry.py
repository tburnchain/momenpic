"""OpenTelemetry setup (§9.1: traces per command → Prometheus·Grafana·Loki via the collector)."""

from __future__ import annotations

from acvm.config import Settings


def setup_tracing(settings: Settings, *, service: str) -> None:
    if not settings.otel_endpoint:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service, "deployment.environment": settings.env}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{settings.otel_endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
