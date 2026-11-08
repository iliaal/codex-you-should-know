import errno
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    every_n_tools: int = 6
    min_interval_seconds: int = 120
    max_checks_per_session: int | None = None
    max_checks_per_hour: int = 30
    timeout_seconds: int = 90
    max_context_chars: int = 60_000
    model: str | None = None
    review_instructions: str = ""
    final_check: bool = True
    retention_days: int = 30
    reply_timeout_seconds: int = 600
    debug: bool = False


def data_directory(override: str | None = None) -> Path:
    try:
        if override or os.environ.get("YSK_DATA_DIR"):
            directory = Path(override or os.environ["YSK_DATA_DIR"]).expanduser().resolve()
        else:
            directory = (
                Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "you-should-know"
            )
        # Non-strict resolve leaves symlink loops unresolved on Python 3.13 and newer.
        # The default CODEX_HOME path is not resolved at all.
        try:
            directory.stat()
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise
        return directory
    except (OSError, RuntimeError) as error:
        raise ValueError(f"Cannot resolve data directory: {error}") from error


def load_config(directory: Path) -> Config:
    path = directory / "config.toml"
    if not path.exists():
        return Config()
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, ValueError) as error:
        raise ConfigError(f"Cannot read {path}: {error}") from error
    unknown = data.keys() - {field.name for field in fields(Config)}
    if unknown:
        raise ConfigError(f"{path}: Unknown configuration keys: {', '.join(sorted(unknown))}")
    for name in ("enabled", "final_check", "debug"):
        if name in data and type(data[name]) is not bool:
            raise ConfigError(f"{path}: {name} must be a boolean")
    bounds = {
        "every_n_tools": (1, 1000),
        "min_interval_seconds": (0, 86_400),
        "max_checks_per_session": (1, 1000),
        "max_checks_per_hour": (1, 1000),
        "timeout_seconds": (1, 105),
        "max_context_chars": (1000, 200_000),
        "retention_days": (1, 3650),
        "reply_timeout_seconds": (1, 86_400),
    }
    for name, (minimum, maximum) in bounds.items():
        if name in data and (type(data[name]) is not int or not minimum <= data[name] <= maximum):
            raise ConfigError(f"{path}: {name} must be an integer between {minimum} and {maximum}")
    if "model" in data and (not isinstance(data["model"], str) or not data["model"].strip()):
        raise ConfigError(f"{path}: model must be a nonempty string")
    if "review_instructions" in data and (
        not isinstance(data["review_instructions"], str) or len(data["review_instructions"]) > 8000
    ):
        raise ConfigError(
            f"{path}: review_instructions must be a string of at most 8000 characters"
        )
    return Config(**data)
