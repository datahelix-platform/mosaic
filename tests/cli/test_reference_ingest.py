"""Integration tests for CLI ingest commands.

The ``mosaic reference install`` legacy pip-flow tests previously housed
here were superseded by the loader-driven lifecycle in PTS-229; their
coverage lives in ``test_reference_install_upgrade.py``.
"""

import os
from pathlib import Path

import pytest
import yaml


class TestIngest:
    """Tests for mosaic ingest command."""

    @pytest.fixture(autouse=True)
    def setup_test_env(self, monkeypatch, tmp_path):
        """Set up test environment with temporary directories."""
        test_data_dir = tmp_path / ".mosaic" / "data"
        test_data_dir.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))

    def test_ingest_with_configured_sources(self, tmp_path):
        """Test ingest with configured data sources."""
        config_path = tmp_path / ".mosaic" / "data" / "sources.yaml"
        config_path.write_text(
            yaml.dump(
                {
                    "sources": [
                        {
                            "name": "test_source",
                            "type": "starlims",
                            "connection": {"host": "localhost"},
                        }
                    ]
                }
            )
        )
        result = os.system(f"python -m mosaic.cli.main ingest --config {config_path}")
        assert result == 0

    def test_ingest_with_no_configured_sources(self):
        """Test ingest with no configured sources shows error."""
        config_path = Path.home() / ".mosaic" / "data" / "sources.yaml"
        if config_path.exists():
            config_path.unlink()
        result = os.system("python -m mosaic.cli.main ingest 2>&1")
        assert result != 0

    def test_ingest_with_empty_sources(self, tmp_path):
        """Test ingest with empty sources list shows error."""
        config_path = tmp_path / ".mosaic" / "data" / "sources.yaml"
        config_path.write_text(yaml.dump({"sources": []}))
        result = os.system(
            f"python -m mosaic.cli.main ingest --config {config_path} 2>&1"
        )
        assert result != 0

    def test_ingest_with_nonexistent_config_file(self):
        """Test ingest with nonexistent config file shows error."""
        result = os.system(
            "python -m mosaic.cli.main ingest --config /nonexistent/config.yaml 2>&1"
        )
        assert result != 0
