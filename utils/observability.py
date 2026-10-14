# utils/observability.py
# OpenTelemetry Cloud Trace + GCP Resource Detection + Structured Cloud Logging
# Built for:
#   - google-cloud-trace==1.21.0
#   - opentelemetry-exporter-gcp-trace==1.15.0
#   - opentelemetry-resourcedetector-gcp==1.14.0

# Imports
from datetime import datetime, timezone
from dotenv import load_dotenv
from google.cloud.trace_v2 import TraceServiceClient
import json
import logging
import os
import sys
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.resourcedetector.gcp_resource_detector import (
    GoogleCloudResourceDetector,
)
from opentelemetry.sdk.resources import (
    OTELResourceDetector,
    Resource,
    get_aggregated_resources,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.attributes.service_attributes import SERVICE_NAME

# Load ENV Vars
load_dotenv()
GCP_PROJECT_ID = os.environ["GCP_PROJECT_ID"]

_initialized = False


################################################################
# Trace-Correlated Structured JSON Formatter for Cloud Logging
################################################################

class CloudLoggingJsonFormatter(logging.Formatter):
    """
    Formats Python logs as single-line JSON recognized by Google Cloud Logging,
    automatically injecting `logging.googleapis.com/trace` and `spanId` from the
    active OpenTelemetry span so logs link directly to Cloud Trace waterfalls.
    """

    def __init__(self, project_id: str) -> None:
        super().__init__()
        self.project_id = project_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Correlate log entry with active OpenTelemetry span in Cloud Trace
        current_span = trace.get_current_span()
        span_ctx = current_span.get_span_context() if current_span else None
        if span_ctx and span_ctx.is_valid and self.project_id:
            trace_id_hex = format(span_ctx.trace_id, "032x")
            span_id_hex = format(span_ctx.span_id, "016x")
            payload["logging.googleapis.com/trace"] = (
                f"projects/{self.project_id}/traces/{trace_id_hex}"
            )
            payload["logging.googleapis.com/spanId"] = span_id_hex
            payload["logging.googleapis.com/trace_sampled"] = bool(
                span_ctx.trace_flags.sampled
            )

        # Attach structured analytics fields if passed via `extra={"telemetry": ...}`
        telemetry = getattr(record, "telemetry", None)
        if isinstance(telemetry, dict):
            payload["event_type"] = telemetry.get("event_type", "APP_EVENT")
            payload["telemetry"] = telemetry

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


################################################################
# OpenTelemetry + Cloud Trace Initialization
################################################################

def setup_observability(service_name: str = "clipper-agent-backend") -> None:
    """
    1. Detects GCP environment attributes (Cloud Run, GCE, etc.) via
       GoogleCloudResourceDetector(raise_on_error=False).
    2. Registers CloudTraceSpanExporter (backed by google-cloud-trace v2 client)
       on the global TracerProvider so all ADK 2.9 agent/LLM/tool spans and
       custom app spans export to Google Cloud Trace.
    3. Configures structured JSON logging with trace correlation.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    # Ensure GOOGLE_CLOUD_PROJECT is set for ADK's internal telemetry helpers
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", GCP_PROJECT_ID)

    # Configure Structured JSON Logging (enabled on Cloud Run or when JSON_LOGS=true)
    use_json_logs = bool(
        os.getenv("K_SERVICE") or os.getenv("JSON_LOGS", "").lower() == "true"
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if use_json_logs:
        formatter = CloudLoggingJsonFormatter(project_id=GCP_PROJECT_ID)
        if not root_logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(formatter)
            root_logger.addHandler(handler)
        else:
            for handler in root_logger.handlers:
                handler.setFormatter(formatter)

    # Configure OpenTelemetry Resource & CloudTraceSpanExporter
    enable_trace = bool(
        os.getenv("K_SERVICE")
        or os.getenv("ENABLE_CLOUD_TRACE", "true").lower() == "true"
    )
    if not enable_trace:
        return

    try:
        # Aggregate GCP resource attributes (Cloud Run revision, region, project)
        # raise_on_error=False prevents metadata server timeouts when running locally
        detected_resource = get_aggregated_resources(
            detectors=[
                GoogleCloudResourceDetector(raise_on_error=False),
                OTELResourceDetector(),
            ],
            initial_resource=Resource.create({SERVICE_NAME: service_name}),
        )

        # Initialize CloudTraceSpanExporter with google-cloud-trace v2 client
        trace_client = TraceServiceClient()
        cloud_trace_exporter = CloudTraceSpanExporter(
            project_id=GCP_PROJECT_ID,
            client=trace_client,
            resource_regex=r".*",
        )
        span_processor = BatchSpanProcessor(cloud_trace_exporter)

        # Attach to existing TracerProvider if already present, otherwise create one
        current_provider = trace.get_tracer_provider()
        if isinstance(current_provider, TracerProvider):
            current_provider.add_span_processor(span_processor)
        else:
            tracer_provider = TracerProvider(resource=detected_resource)
            tracer_provider.add_span_processor(span_processor)
            trace.set_tracer_provider(tracer_provider)

        logging.getLogger(__name__).info(
            f"OpenTelemetry CloudTraceSpanExporter enabled for project '{GCP_PROJECT_ID}'."
        )
    except Exception as e:
        logging.getLogger(__name__).warning(
            f"Failed to initialize CloudTraceSpanExporter: {e}"
        )


def get_tracer(name: str = "clipper.backend") -> trace.Tracer:
    """Returns an OpenTelemetry Tracer tied to the global TracerProvider."""
    return trace.get_tracer(name)


################################################################
# Structured Analytics Event Logger (TDD Page 6 Metrics)
################################################################

analytics_logger = logging.getLogger("clipper.analytics")


def log_analytics_event(event_type: str, **fields: Any) -> None:
    """
    Emits a trace-correlated structured JSON event for Cloud Logging & Log-Based Metrics.
    """
    telemetry_payload = {"event_type": event_type, **fields}
    analytics_logger.info(
        f"[{event_type}] {json.dumps(fields, default=str)}",
        extra={"telemetry": telemetry_payload},
    )