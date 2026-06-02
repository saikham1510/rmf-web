from enum import Enum

from tortoise.contrib.pydantic.creator import pydantic_model_creator
from tortoise.fields import BigIntField, CharEnumField, CharField, TextField
from tortoise.models import Model


class Alert(Model):
    """
    General alert that can be triggered by events.
    """

    class Category(str, Enum):
        Default = "default"
        Task = "task"
        Fleet = "fleet"
        Robot = "robot"
        Cleaning = "cleaning"
        Fire_Critical = "fire_critical"
        Fire_Warning = "fire_warning"
        Fire_Info = "fire_info"
        Security_Threat = "security_threat"
        Semantic = "semantic"

    id = CharField(255, pk=True)
    original_id = CharField(255, index=True)
    category = CharEnumField(Category, index=True)
    severity = CharField(50, null=True, index=True)  # e.g. critical, warning, info
    source_type = CharField(50, null=True)  # e.g. semantic, patrol, perception_bridge
    dedup_key = CharField(255, null=True, index=True)  # for deduplication
    unix_millis_created_time = BigIntField(null=False, index=True)
    acknowledged_by = CharField(255, null=True, index=True)
    unix_millis_acknowledged_time = BigIntField(null=True, index=True)


AlertPydantic = pydantic_model_creator(Alert)
