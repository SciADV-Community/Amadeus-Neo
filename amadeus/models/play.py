from dataclasses import dataclass


@dataclass(frozen=True)
class PlayConfig:
    guild_id: int
    forum_channel_id: int | None = None
    auto_archive_duration: int | None = None


@dataclass(frozen=True)
class PlayGame:
    guild_id: int
    key: str
    display_name: str
    forum_channel_id: int | None = None
    forum_tag_id: int | None = None
    enabled: bool = True
