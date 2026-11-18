from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Result:
    state: str                      # scheduled | published
    remote_id: str = ""
    remote_url: str = ""
    extra: dict = field(default_factory=dict)
