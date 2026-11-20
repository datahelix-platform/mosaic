"""Data source configuration management."""

import json
from pathlib import Path
from typing import Any

import yaml


def get_config_dir() -> Path:
    """Get the Mosaic configuration directory."""
    data_dir = Path.home() / ".mosaic" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_sources_config_path() -> Path:
    """Get the path to the data sources configuration file."""
    return get_config_dir() / "sources.yaml"


class DataSourceConfig:
    """Configuration for an external data source."""

    def __init__(
        self,
        name: str,
        type: str,
        connection: dict[str, Any],
        options: dict[str, Any] | None = None,
    ):
        self.name = name
        self.type = type
        self.connection = connection
        self.options = options or {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DataSourceConfig":
        """Create a DataSourceConfig from a dictionary."""
        return cls(
            name=data["name"],
            type=data["type"],
            connection=data.get("connection", {}),
            options=data.get("options", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "type": self.type,
            "connection": self.connection,
            "options": self.options,
        }


class DataSources:
    """Manager for data source configurations."""

    def __init__(self, sources: list[DataSourceConfig] | None = None):
        self.sources = sources or []

    @classmethod
    def load(cls, config_path: Path | None = None) -> "DataSources":
        """Load data sources from configuration file."""
        if config_path is None:
            config_path = get_sources_config_path()

        if not config_path.exists():
            return cls([])

        content = config_path.read_text()

        if config_path.suffix == ".json":
            data = json.loads(content)
        else:
            data = yaml.safe_load(content)

        if not data or "sources" not in data:
            return cls([])

        sources = [DataSourceConfig.from_dict(s) for s in data["sources"]]
        return cls(sources)

    def save(self, config_path: Path | None = None) -> None:
        """Save data sources to configuration file."""
        if config_path is None:
            config_path = get_sources_config_path()

        data = {"sources": [s.to_dict() for s in self.sources]}

        if config_path.suffix == ".json":
            config_path.write_text(json.dumps(data, indent=2))
        else:
            config_path.write_text(yaml.dump(data, default_flow_style=False))

    def add(self, source: DataSourceConfig) -> None:
        """Add a data source."""
        for existing in self.sources:
            if existing.name == source.name:
                raise ValueError(f"Source '{source.name}' already exists")
        self.sources.append(source)

    def remove(self, name: str) -> None:
        """Remove a data source by name."""
        self.sources = [s for s in self.sources if s.name != name]

    def get(self, name: str) -> DataSourceConfig | None:
        """Get a data source by name."""
        for source in self.sources:
            if source.name == name:
                return source
        return None

    def validate(self) -> list[str]:
        """Validate all data sources. Returns list of error messages."""
        errors = []
        for source in self.sources:
            if not source.name:
                errors.append("Source name is required")
            if not source.type:
                errors.append(f"Source '{source.name}': type is required")
            if not source.connection:
                errors.append(f"Source '{source.name}': connection is required")
            # Additional validation for specific sources types could be added here
            # but we'll leave it generic as this is the core validation

        return errors
