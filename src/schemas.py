from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class PluginConfig:
    stream_title: str = "Sorry, this channel is unavailable."
    stream_description: str = "While this channel is not currently available, here are some other channels you can watch."
    stream_channel_cols: int = 5
    tms_image_path: Optional[str] = None
    tms_log_level: str = "INFO"

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            stream_title=str(data.get("stream_title", cls.stream_title)),
            stream_description=str(data.get("stream_description", cls.stream_description)),
            stream_channel_cols=int(data.get("stream_channel_cols", cls.stream_channel_cols)),
            tms_image_path=data.get("tms_image_path", cls.tms_image_path),
            tms_log_level=str(data.get("tms_log_level", cls.tms_log_level)).upper()
        )

    def dict(self):
        return asdict(self)
