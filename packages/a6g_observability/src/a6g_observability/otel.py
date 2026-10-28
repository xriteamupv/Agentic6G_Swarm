"""OpenTelemetry setup. Without the ``otel`` extra installed, spans are no-ops."""

from opentelemetry import trace


def setup_tracing(service_name: str, otlp_endpoint: str | None = None) -> bool:
    """Install an SDK tracer provider exporting to ``otlp_endpoint`` (OTLP/HTTP).

    Returns False (tracing stays a no-op) when no endpoint is configured.
    """
    if not otlp_endpoint:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise RuntimeError("install a6g-observability[otel] to export traces") from exc
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    trace.set_tracer_provider(provider)
    return True
