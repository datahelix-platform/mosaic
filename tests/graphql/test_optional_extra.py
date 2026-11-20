"""Optional-extra plumbing and SDK→GraphQL error mapping tests."""

from __future__ import annotations

import sys

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import (
    ConfigError,
    EntityAlreadySupersededError,
    EntityNotFoundError,
    ValidationError,
    ValidationFailed,
    ValidationFailure,
)
from mosaic.graphql import (
    GRAPHQL_EXTRA_HINT,
    build_graphql_schema,
    create_graphql_router,
    graphql_available,
)


class TestExtraDetection:
    def test_graphql_available_when_installed(self):
        assert graphql_available() is True

    def test_missing_extra_raises_actionable_import_error(self, monkeypatch, registry):
        # ``sys.modules[name] = None`` makes ``import name`` raise
        # ImportError — simulates an environment without the extra.
        monkeypatch.setitem(sys.modules, "strawberry", None)
        assert graphql_available() is False
        with pytest.raises(ImportError, match="pip install 'datahelix-mosaic\\[graphql\\]'"):
            build_graphql_schema(registry)
        with pytest.raises(ImportError, match="pip install 'datahelix-mosaic\\[graphql\\]'"):
            create_graphql_router(MosaicClient())
        assert "graphql" in GRAPHQL_EXTRA_HINT

    def test_schemaless_client_raises_config_error(self):
        with pytest.raises(ConfigError, match="schema-backed"):
            create_graphql_router(MosaicClient())

    def test_create_default_app_requires_registry_for_graphql(self):
        from mosaic.serve import create_default_app

        with pytest.raises(ConfigError):
            create_default_app(mosaic_client=MosaicClient(), graphql=True)


class TestErrorMapping:
    """SDK exceptions map onto structured GraphQL error extensions."""

    def _map(self, exc):
        from mosaic.graphql.resolvers import _as_graphql_error

        return _as_graphql_error(exc)

    def test_validation_failed_carries_tier_envelope(self):
        from mosaic.core.validation.validators import (
            ValidationFailure as TierFailure,
            ValidationResult,
        )

        result = ValidationResult(
            is_valid=False,
            failures=[
                TierFailure(
                    tier="cel",
                    rule="volume_positive",
                    field="volume_ml",
                    message="must be > 0",
                )
            ]
        )
        error = self._map(ValidationFailed(message="validation failed", result=result))
        assert error.extensions["code"] == "VALIDATION_FAILED"
        assert error.extensions["passed"] is False
        failure = error.extensions["failures"][0]
        assert failure["tier"] == "cel"
        assert failure["rule"] == "volume_positive"
        assert failure["field"] == "volume_ml"

    def test_validation_failure_carries_rule_context(self):
        error = self._map(
            ValidationFailure(
                message="bad write",
                rule_id="rule-7",
                entity_type="Sample",
                entity_id="s-1",
            )
        )
        assert error.extensions["code"] == "VALIDATION_FAILED"
        assert error.extensions["rule_id"] == "rule-7"
        assert error.extensions["entity_type"] == "Sample"
        assert error.extensions["entity_id"] == "s-1"

    def test_validation_error(self):
        error = self._map(ValidationError(message="nope"))
        assert error.extensions["code"] == "VALIDATION_FAILED"

    def test_not_found(self):
        error = self._map(EntityNotFoundError(message="Entity not found: x"))
        assert error.extensions["code"] == "NOT_FOUND"

    def test_already_superseded(self):
        error = self._map(EntityAlreadySupersededError(message="already superseded"))
        assert error.extensions["code"] == "ALREADY_SUPERSEDED"

    def test_unexpected_exception_is_internal_error(self):
        error = self._map(RuntimeError("boom"))
        assert error.extensions["code"] == "INTERNAL_ERROR"
