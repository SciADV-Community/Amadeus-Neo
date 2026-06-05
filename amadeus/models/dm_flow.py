from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DmFlow:
    guild_id: int
    user_id: int
    flow_type: str
    state: str
    # Arbitrary step data stored as JSON (role name, image bytes, etc.)
    data: dict = field(default_factory=dict)
    started_at: datetime | None = None
    updated_at: datetime | None = None
