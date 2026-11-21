import re
import os
from typing import Any, Optional

from mosaic.core.exceptions import (
    AdapterError,
    ConfigError,
    EntityNotFoundError,
    SchemaError,
    ValidationError,
)


ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def substitute_env_vars(value: Any, max_depth: int = 10) -> Any:
    if isinstance(value, str):
        depth = 0
        current = value
        while depth < max_depth:
            match = ENV_VAR_PATTERN.search(current)
            if not match:
                break
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                raise ConfigError(
                    f"Environment variable '{var_name}' is not defined",
                    field_name=var_name,
                )
            current = current[: match.start()] + env_value + current[match.end() :]
            depth += 1
        return current
    elif isinstance(value, dict):
        return {k: substitute_env_vars(v, max_depth) for k, v in value.items()}
    elif isinstance(value, list):
        return [substitute_env_vars(item, max_depth) for item in value]
    return value
