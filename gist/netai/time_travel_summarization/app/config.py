import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand_env(value: str) -> str:
    if not isinstance(value, str):
        return value
    return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class ExtensionConfig:
    config_path: Path
    data_path: str = "./data/merged_trajectory.csv"
    auto_generate: bool = False
    astronaut_usd: str = ""
    prim_map: Dict[str, str] = field(default_factory=dict)
    event_summary: List[str] = field(default_factory=list)

    @property
    def config_dir(self) -> Path:
        return self.config_path.parent

    @property
    def data_uri(self) -> str:
        """Return data_path as a URI.

        Existing URI values are returned unchanged. Local paths are resolved against
        the config directory before conversion to file:// URIs.
        """
        raw = self.data_path
        if "://" in raw:
            return raw
        path = Path(raw)
        if not path.is_absolute():
            path = (self.config_dir / raw.lstrip("./")).resolve()
        return path.resolve().as_uri()

    @classmethod
    def from_file(cls, config_path: str) -> "ExtensionConfig":
        path = Path(config_path)
        _load_dotenv(path.parent / ".env")
        with open(path, "r", encoding="utf-8") as file:
            raw = json.load(file)

        return cls(
            config_path=path,
            data_path=_expand_env(raw.get("data_path", "./data/merged_trajectory.csv")),
            auto_generate=raw.get("auto_generate", False),
            astronaut_usd=_expand_env(raw.get("astronaut_usd", "")),
            prim_map=dict(raw.get("prim_map", {})),
            event_summary=list(raw.get("event_summary", [])),
        )

    def resolve_from_config(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return self.config_dir / value.lstrip("./")

    def resolve_data_path(self, module_dir: Path) -> Path:
        raw = self.data_path
        if "://" in raw:
            from urllib.parse import urlparse

            parsed = urlparse(raw)
            if parsed.scheme == "file":
                return Path(parsed.path)
            raise ValueError(
                f"resolve_data_path() returns a Path and cannot handle scheme {parsed.scheme!r}; "
                f"use ExtensionConfig.data_uri or TrajectoryRepository.load_from_uri instead."
            )
        path = Path(raw)
        if path.is_absolute():
            return path
        return module_dir / raw.lstrip("./")
