import os
import tempfile
from pathlib import Path

import pytest
import yaml

from mosaic.config import (
    MosaicConfig,
    ConfigError,
    ValidationError,
    load_mosaic_config,
    substitute_env_vars,
)


class TestSubstituteEnvVars:
    def test_simple_string_substitution(self):
        os.environ["TEST_VAR"] = "hello"
        result = substitute_env_vars("prefix ${TEST_VAR} suffix")
        assert result == "prefix hello suffix"

    def test_no_env_vars_returns_same(self):
        result = substitute_env_vars("plain string")
        assert result == "plain string"

    def test_undefined_env_var_raises_config_error(self):
        os.environ.pop("UNDEFINED_VAR", None)
        with pytest.raises(ConfigError) as exc_info:
            substitute_env_vars("value ${UNDEFINED_VAR}")
        assert "UNDEFINED_VAR" in str(exc_info.value)
        assert exc_info.value.field_name == "UNDEFINED_VAR"

    def test_nested_env_vars(self):
        os.environ["OUTER"] = "${INNER}"
        os.environ["INNER"] = "resolved"
        result = substitute_env_vars("${OUTER}")
        assert result == "resolved"

    def test_dict_substitution(self):
        os.environ["DB_HOST"] = "localhost"
        result = substitute_env_vars({"host": "${DB_HOST}", "port": 5432})
        assert result == {"host": "localhost", "port": 5432}

    def test_list_substitution(self):
        os.environ["ITEM1"] = "a"
        os.environ["ITEM2"] = "b"
        result = substitute_env_vars(["${ITEM1}", "${ITEM2}", "c"])
        assert result == ["a", "b", "c"]


class TestMosaicConfigModel:
    def test_valid_config(self):
        config = MosaicConfig(schema_path="/path/to/schema")
        assert config.schema_path == Path("/path/to/schema")

    def test_schema_path_as_string(self):
        config = MosaicConfig(schema_path="/path/to/schema")
        assert isinstance(config.schema_path, Path)

    def test_optional_fields(self):
        config = MosaicConfig(
            schema_path="/schema",
            storage_backend="sqlite",
            database_url="sqlite:///db.db",
        )
        assert config.storage_backend == "sqlite"
        assert config.database_url == "sqlite:///db.db"

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            MosaicConfig(schema_path="/schema", unknown_field="value")


class TestLoadMosaicConfig:
    def test_valid_yaml_without_env_vars(self, tmp_path):
        config_file = tmp_path / "mosaic.yaml"
        config_file.write_text("schema_path: /path/to/schema\n")

        config = load_mosaic_config(config_file)
        assert config.schema_path == Path("/path/to/schema")

    def test_valid_yaml_with_env_vars(self, tmp_path):
        os.environ["SCHEMA_PATH"] = "/env/schema"
        config_file = tmp_path / "mosaic.yaml"
        config_file.write_text("schema_path: ${SCHEMA_PATH}\n")

        config = load_mosaic_config(config_file)
        assert config.schema_path == Path("/env/schema")

    def test_undefined_env_var_error(self, tmp_path):
        os.environ.pop("NONEXISTENT", None)
        config_file = tmp_path / "mosaic.yaml"
        config_file.write_text("schema_path: ${NONEXISTENT}\n")

        with pytest.raises(ConfigError) as exc_info:
            load_mosaic_config(config_file)
        assert "NONEXISTENT" in str(exc_info.value)

    def test_missing_required_field_error(self, tmp_path):
        config_file = tmp_path / "mosaic.yaml"
        config_file.write_text("storage_backend: sqlite\n")

        with pytest.raises(ConfigError) as exc_info:
            load_mosaic_config(config_file)
        assert "schema_path" in str(exc_info.value)

    def test_incorrect_type_error(self, tmp_path):
        config_file = tmp_path / "mosaic.yaml"
        config_file.write_text("schema_path: 12345\n")

        with pytest.raises(ValidationError) as exc_info:
            load_mosaic_config(config_file)
        assert "schema_path" in str(exc_info.value)

    def test_file_not_found_error(self):
        with pytest.raises(ConfigError) as exc_info:
            load_mosaic_config("/nonexistent/path/mosaic.yaml")
        assert "not found" in str(exc_info.value)

    def test_invalid_yaml_error(self, tmp_path):
        config_file = tmp_path / "mosaic.yaml"
        config_file.write_text("invalid: yaml: content: [}\n")

        with pytest.raises(ConfigError) as exc_info:
            load_mosaic_config(config_file)
        assert "invalid yaml" in str(exc_info.value).lower()


class TestErrorMessages:
    def test_config_error_includes_field_name(self):
        err = ConfigError("Missing required field", field_name="schema_path")
        assert "schema_path" in str(err)
        assert err.field_name == "schema_path"

    def test_validation_error_includes_type_info(self):
        err = ValidationError("Type mismatch", expected_type="string", actual_value=123)
        assert "string" in str(err)
        assert err.actual_value == 123
