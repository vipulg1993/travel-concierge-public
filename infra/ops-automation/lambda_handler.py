"""Routes CloudWatch alarm notifications into the operations backlog.

Receives alarm state-change events via SNS, formats a concise human-readable
summary, and opens an investigation task in the operations platform so an
on-call responder (human or automated) can pick it up.

Payload shape (SNS → Lambda):
    event["Records"][*]["Sns"]["Message"]  = JSON string of CloudWatch alarm event
"""

import json
import logging
import os
import re
from datetime import datetime

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_SPACE_ID = os.environ["AGENT_SPACE_ID"]
ASSOCIATION_ID = os.environ["ASSOCIATION_ID"]
REGION = os.environ.get("AWS_REGION", "us-east-1")

_devops = boto3.client("devops-agent", region_name=REGION)


# ─────────────────────────────────────────────────────────────────────────────
# Service naming — maps technical resource names to human-friendly service
# labels used in task titles. Keeps the backlog readable for responders who
# aren't familiar with every FunctionName.
# ─────────────────────────────────────────────────────────────────────────────
SERVICE_LABELS = {
    "travel-concierge-weather-provider": "Weather forecast service",
    "travel-concierge-flights": "Flight catalog service",
    "travel-concierge-hotels": "Hotel catalog service",
}


def _service_label(dimensions: list) -> str:
    """Pick a human-readable label from alarm dimensions, falling back to the raw name."""
    for d in dimensions:
        v = d.get("value") or d.get("Value")
        if v and v in SERVICE_LABELS:
            return SERVICE_LABELS[v]
    # Fall back to the first dimension value, cleaned up
    for d in dimensions:
        v = d.get("value") or d.get("Value")
        if v:
            return v
    return "Unknown service"


def _priority_from_alarm(alarm: dict) -> str:
    """Map alarm severity to backlog priority.

    CloudWatch alarms carry no built-in severity, so we infer from
    (a) comparison operator and threshold magnitude, and (b) known
    criticality of the metric. Latency SLOs and 5xx counts are HIGH;
    informational counts are MEDIUM.
    """
    metric = alarm.get("Trigger", {}).get("MetricName", "").lower()
    namespace = alarm.get("Trigger", {}).get("Namespace", "").lower()

    # Latency breaches on request-serving infra are HIGH impact.
    if metric in {"duration", "latency", "targetresponsetime", "responsetime"}:
        return "HIGH"
    # Error-rate breaches are CRITICAL since they indicate user-visible failures.
    if "error" in metric or "fault" in metric or metric.endswith("exceptions"):
        return "CRITICAL"
    # Throttles / capacity issues typically HIGH.
    if "throttle" in metric or "capacity" in metric:
        return "HIGH"
    return "MEDIUM"


def _format_title(alarm: dict, service_label: str) -> str:
    """Build a concise backlog task title.

    Pattern: '<Service>: <condition>' — e.g. 'Weather forecast service: elevated response time'
    """
    metric = alarm.get("Trigger", {}).get("MetricName", "unknown metric")
    metric_lower = metric.lower()

    if metric_lower == "duration":
        return f"{service_label}: elevated response time"
    if metric_lower in {"errors", "faults"}:
        return f"{service_label}: error rate above baseline"
    if "throttle" in metric_lower:
        return f"{service_label}: request throttling detected"
    if "concurrent" in metric_lower:
        return f"{service_label}: concurrency ceiling approaching"
    # Generic fallback
    return f"{service_label}: {metric} threshold breach"


def _format_description(alarm: dict) -> str:
    """Build a detailed description that gives the responder (or an automated
    investigation agent) enough context to start without re-fetching the alarm.
    """
    trigger = alarm.get("Trigger", {})
    metric = trigger.get("MetricName", "unknown")
    namespace = trigger.get("Namespace", "unknown")
    statistic = trigger.get("Statistic", trigger.get("ExtendedStatistic", "Average"))
    period = trigger.get("Period", 60)
    threshold = trigger.get("Threshold")
    comparison = trigger.get("ComparisonOperator", "GreaterThanThreshold")
    eval_periods = trigger.get("EvaluationPeriods", 1)
    dims = trigger.get("Dimensions", [])

    dims_str = ", ".join(
        f"{(d.get('name') or d.get('Name'))}={d.get('value') or d.get('Value')}"
        for d in dims
    ) or "(none)"

    # Convert comparison operator to prose
    op_prose = {
        "GreaterThanThreshold": "exceeded",
        "GreaterThanOrEqualToThreshold": "met or exceeded",
        "LessThanThreshold": "dropped below",
        "LessThanOrEqualToThreshold": "fell to or below",
    }.get(comparison, "crossed")

    # Human-readable timestamp
    state_change = alarm.get("StateChangeTime", "")
    try:
        ts = datetime.fromisoformat(state_change.replace("Z", "+00:00"))
        when = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        when = state_change or "unknown time"

    unit = _duration_unit_hint(metric, threshold)

    description = f"""Alarm {alarm.get('AlarmName', '?')} transitioned to ALARM state at {when}.

Signal
    Metric:        {namespace} / {metric}
    Dimensions:    {dims_str}
    Statistic:     {statistic} over {period}s window
    Condition:     {statistic} {op_prose} {threshold}{unit}
    Confirmed by:  {eval_periods} consecutive evaluation period(s)

State transition reason reported by CloudWatch:
    {alarm.get('NewStateReason', '(none provided)')}

Expected next step: identify the root cause of the signal change, confirm
whether it reflects a legitimate degradation or a benign one-off, and
recommend remediation if degradation is confirmed. Correlating sources
include the service's own CloudWatch Logs, recent deployments or config
changes (CloudTrail), the service's X-Ray traces for the affected time
window, and upstream dependency metrics.

Alarm:    {_console_url(alarm)}
ARN:      {alarm.get('AlarmArn', '?')}
Account:  {alarm.get('AWSAccountId', '?')}
Region:   {alarm.get('Region', '?')}
"""
    return description.strip()


def _duration_unit_hint(metric: str, threshold) -> str:
    """Attach a unit suffix for common metrics so the description reads naturally."""
    if threshold is None:
        return ""
    m = metric.lower()
    if m == "duration":
        return "ms"
    if m in {"targetresponsetime", "responsetime"}:
        return "s"
    return ""


def _console_url(alarm: dict) -> str:
    """Deep-link to the alarm in the CloudWatch console."""
    name = alarm.get("AlarmName", "")
    region = _region_slug(alarm.get("Region", ""))
    # URL-encode the alarm name
    encoded = re.sub(r"[^A-Za-z0-9]", lambda m: "%" + format(ord(m.group(0)), "02X"), name)
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
        f"#alarmsV2:alarm/{encoded}"
    )


def _region_slug(human_region: str) -> str:
    """Convert 'US East (N. Virginia)' → 'us-east-1'. Falls back to the env region."""
    mapping = {
        "US East (N. Virginia)": "us-east-1",
        "US East (Ohio)": "us-east-2",
        "US West (N. California)": "us-west-1",
        "US West (Oregon)": "us-west-2",
        "Europe (Ireland)": "eu-west-1",
        "Europe (Frankfurt)": "eu-central-1",
    }
    return mapping.get(human_region, REGION)


# ─────────────────────────────────────────────────────────────────────────────
# Lambda entry point
# ─────────────────────────────────────────────────────────────────────────────
def lambda_handler(event, _context):
    created = []
    skipped = []

    for record in event.get("Records", []):
        try:
            sns = record.get("Sns") or {}
            raw = sns.get("Message", "")
            if not raw:
                skipped.append("empty SNS message")
                continue

            alarm = json.loads(raw)

            # Only act on ALARM state transitions (not OK or INSUFFICIENT_DATA).
            new_state = alarm.get("NewStateValue")
            if new_state != "ALARM":
                skipped.append(f"state={new_state} (ignored)")
                logger.info("Skipping non-ALARM transition: %s", new_state)
                continue

            service_label = _service_label(alarm.get("Trigger", {}).get("Dimensions", []))
            title = _format_title(alarm, service_label)
            description = _format_description(alarm)
            priority = _priority_from_alarm(alarm)

            # Note: the optional `reference` field requires a registered
            # external-system serviceId (one of: eventChannel, pagerduty,
            # servicenow, slack, etc.). CloudWatch isn't on that list and we
            # don't have an eventChannel service registered, so we omit the
            # reference block. All alarm metadata — including the console
            # URL — is included in the task description instead.
            resp = _devops.create_backlog_task(
                agentSpaceId=AGENT_SPACE_ID,
                taskType="INVESTIGATION",
                title=title,
                description=description,
                priority=priority,
            )
            task_id = resp.get("task", {}).get("taskId")
            created.append(task_id)
            logger.info(
                "Opened investigation %s (priority=%s) for alarm %s",
                task_id, priority, alarm.get("AlarmName"),
            )

        except Exception as e:
            logger.exception("Failed to process SNS record: %s", e)
            skipped.append(f"error: {e}")

    return {
        "created": created,
        "skipped": skipped,
    }
