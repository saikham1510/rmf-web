"""Single source of truth for the app-level task priority scheme.

The dashboard, digiBASE bridge and schedules all submit
``priority: {"type": "binary", "value": 0|1|2}`` (Normal/Urgent/Critical).
RMF core only understands a binary priority (0 = low, >0 = high), so Urgent
and Critical are identical to the task planner; the Critical level's extra
power (preempting a running task) is enforced by the api-server using the
helpers in this module.
"""

from typing import List, Optional

NORMAL_PRIORITY_VALUE = 0
URGENT_PRIORITY_VALUE = 1
CRITICAL_PRIORITY_VALUE = 2

PRIORITY_LABEL_VALUES = {
    "normal": NORMAL_PRIORITY_VALUE,
    "urgent": URGENT_PRIORITY_VALUE,
    "critical": CRITICAL_PRIORITY_VALUE,
}


def task_priority_label(labels: Optional[List[str]]) -> Optional[str]:
    if not labels:
        return None

    priority_label = None
    for label in labels:
        normalized = label.lower().strip()
        if normalized in ("critical=true", "preempt=interrupt", "priority=critical"):
            return "critical"
        if normalized in ("urgent=true", "priority=urgent"):
            priority_label = priority_label or "urgent"
            continue
        if normalized in ("normal=true", "priority=normal"):
            priority_label = priority_label or "normal"
            continue
        if normalized == "critical":
            return "critical"
        if normalized == "urgent":
            priority_label = priority_label or "urgent"
            continue
        if normalized == "normal":
            priority_label = priority_label or "normal"
    return priority_label


def priority_value(
    task_request: object = None, labels: Optional[List[str]] = None
) -> int:
    """Resolve a task's priority level from its request and/or labels.

    The ``priority`` dict on the request wins; labels are the fallback for
    requests submitted with only priority labels. Unknown or missing
    priority resolves to Normal.
    """
    priority = getattr(task_request, "priority", None)
    if isinstance(priority, dict):
        value = priority.get("value")
        if isinstance(value, int) and not isinstance(value, bool):
            return value

    if labels is None:
        labels = getattr(task_request, "labels", None)
    label = task_priority_label(labels)
    if label is not None:
        return PRIORITY_LABEL_VALUES[label]
    return NORMAL_PRIORITY_VALUE


def can_preempt(incoming: int, victim: int) -> bool:
    """Only Critical (and above) may interrupt, and never a peer or better."""
    return incoming >= CRITICAL_PRIORITY_VALUE and victim < incoming
