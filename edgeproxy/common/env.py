"""Read secrets from the environment, falling back to the repo's git-ignored .env file."""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).parents[2] / ".env"


def get_secret(name: str, env_file: Path = ENV_FILE) -> str:
    value = os.environ.get(name, "").strip()
    if not value and env_file.exists():
        for line in env_file.read_text().splitlines():
            key, sep, rest = line.partition("=")
            if sep and key.strip() == name:
                value = rest.strip().strip("\"'")
                break
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in, or export {name}."
        )
    return value
