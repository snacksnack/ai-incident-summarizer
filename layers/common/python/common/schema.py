from dataclasses import dataclass, asdict
from typing import Literal


@dataclass
class NormalizedAlert:
    alert_id: str
    source: Literal["cloudwatch", "datadog", "github"]
    alert_name: str
    affected_service: str
    severity: Literal["critical", "high", "medium", "low"]
    status: Literal["open", "resolved"]
    raw_payload: dict
    received_at: str  # ISO 8601

    def to_dict(self) -> dict:
        return asdict(self)
