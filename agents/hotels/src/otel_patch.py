"""Force Strands' tracer to mirror gen_ai event content onto span attributes
AND attach a lightweight span processor that forces each chat span to
materialize its attributes onto the TracerProvider's active processor chain.

Why this exists
---------------
Strands 1.35's `Tracer._add_event` gates attribute-mirroring on
`is_langfuse`, which checks whether a Langfuse endpoint env var is set:

    def _add_event(self, span, event_name, event_attributes, to_span_attributes=False):
        if to_span_attributes and event_attributes:
            span.set_attributes(event_attributes)   # ← only runs when is_langfuse=True
        span.add_event(event_name, attributes=event_attributes)

Without this attribute-mirroring, `gen_ai.input.messages` and
`gen_ai.output.messages` are emitted only as OTel span EVENTS, which the
AWS ADOT → X-Ray pipeline does not surface in Omni's trace detail panel
(which reads span attributes). Omni then renders "No request or response
content was captured".

The monkey-patch below forces `Tracer.is_langfuse` to True so Strands
duplicates each gen_ai event onto span attributes. The companion env var
`LANGFUSE_BASE_URL=langfuse` (set in infra/deploy.py) is the designed-for
hook Strands offers; experience in AgentCore has shown the env var alone
isn't sufficient for the attributes to reach X-Ray — adding the attached
no-op SpanProcessor appears to force the attributes to materialize on the
exported span. Both are kept as belt-and-suspenders.

This is NOT the standard way to configure instrumentation (env vars and
public configuration APIs are preferred). We ship it because Strands
offers no public toggle AND the env-var path alone isn't reliable in this
AgentCore environment.

Import this module BEFORE constructing any Strands Agent.
"""

import logging as _logging

_log = _logging.getLogger(__name__)

# Primary content-capture mechanism: the env var LANGFUSE_BASE_URL=langfuse,
# set in infra/deploy.py — Strands checks for the substring "langfuse" in a
# handful of env vars to decide `is_langfuse`.
#
# Secondary (belt-and-suspenders) mechanism: the monkey-patch below. Wrapped
# in a broad try/except so a future Strands refactor that renames or removes
# `Tracer.is_langfuse` doesn't crash agent startup — we lose the backup, but
# the env-var mechanism continues to work and content capture is preserved.
# If this patch stops being effective in a future upgrade, the warning below
# will fire at startup and point whoever is debugging at the right file.
try:
    from strands.telemetry.tracer import Tracer as _StrandsTracer

    if not hasattr(_StrandsTracer, "is_langfuse"):
        raise AttributeError(
            "Strands Tracer no longer exposes 'is_langfuse' — the content-"
            "mirror code path has likely been refactored. Recheck "
            "strands/telemetry/tracer.py in the installed Strands version."
        )
    _StrandsTracer.is_langfuse = property(lambda self: True)  # type: ignore[assignment]
except Exception as _e:  # pragma: no cover - defensive
    _log.warning(
        "otel_patch: Strands Tracer monkey-patch skipped (%s: %s). "
        "LANGFUSE_BASE_URL=langfuse remains the primary content-capture "
        "driver, so capture should still work; if Omni starts showing "
        "'No content captured' on chat spans, update this file to match "
        "the new Strands internals.",
        type(_e).__name__, _e,
    )

# Attach a no-op SpanProcessor to the active TracerProvider. In this
# environment, having a user-added processor appears to keep gen_ai span
# attributes present through the AWS Distro → X-Ray export; without it,
# attributes set by Strands' Tracer don't reach X-Ray.
try:
    from opentelemetry import trace as _trace_api
    from opentelemetry.sdk.trace import SpanProcessor as _SpanProcessor

    class _NoOpProcessor(_SpanProcessor):
        def on_start(self, span, parent_context=None):
            pass
        def on_end(self, span):
            pass
        def shutdown(self):
            pass
        def force_flush(self, timeout_millis=30000):
            return True

    _provider = _trace_api.get_tracer_provider()
    if hasattr(_provider, "add_span_processor"):
        _provider.add_span_processor(_NoOpProcessor())
except Exception as _e:  # pragma: no cover - defensive
    _log.warning("otel_patch: failed to attach no-op processor: %r", _e)

_log.info("otel_patch loaded (Strands content-capture adjustments)")
