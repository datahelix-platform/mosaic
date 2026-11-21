## Reference: `mosaic.yaml` Configuration Schema

**Document status:** Draft v0.1
**Depends on:** sec2_architecture.md

This document is the authoritative reference for all valid fields in `mosaic.yaml`.
All top-level sections are optional unless marked required. Unknown keys at any level
produce a `ConfigError` at startup — no silently-ignored config.

---

### Complete Annotated Reference

```yaml
# mosaic.yaml

# ---------------------------------------------------------------------------
# adapter (required)
# Which storage backend to use and its connection settings.
# ---------------------------------------------------------------------------
adapter:
  type: sqlite            # required — "sqlite" | "postgres" (future) | "dynamodb" (future)

  sqlite:                 # required when type = sqlite
    path: ./mosaic.db      # required — file path or ":memory:" for in-memory (testing only)
    synchronous: NORMAL   # optional — "NORMAL" | "FULL" | "OFF"; default: NORMAL
                          # NORMAL: safe for most deployments; FULL: strictest durability

  # Future adapter blocks (not implemented in v0.1):
  #
  # postgres:
  #   host: localhost       # required
  #   port: 5432            # optional; default: 5432
  #   database: mosaic      # required
  #   user: mosaic          # required
  #   password: "${MOSAIC_DB_PASSWORD}"  # env var substitution supported (see §Config Env Vars)
  #   pool_size: 5          # optional; default: 5
  #   pool_timeout: 30      # optional; seconds; default: 30
  #   ssl: false            # optional; default: false
  #
  # dynamodb:
  #   region: us-east-1     # required
  #   table_prefix: mosaic_ # optional; default: "mosaic_"

# ---------------------------------------------------------------------------
# schema (required)
# Path to the entity schema definition file (LinkML format).
# ---------------------------------------------------------------------------
schema:
  path: ./schema.yaml     # required — relative to mosaic.yaml location or absolute

# ---------------------------------------------------------------------------
# validators (optional)
# Path to the config-driven business rule validators file.
# Omit this section entirely if no config validators are needed.
# ---------------------------------------------------------------------------
validators:
  path: ./validators.yaml # required if section is present

# ---------------------------------------------------------------------------
# server (optional)
# REST API server settings. Only used when running `mosaic serve`.
# ---------------------------------------------------------------------------
server:
  host: 127.0.0.1         # optional; default: 127.0.0.1
                          # use 0.0.0.0 to bind all interfaces (caution: no auth in v0.1)
  port: 8000              # optional; default: 8000
  workers: 1              # optional; default: 1
                          # >1 workers requires a thread-safe adapter (SQLite WAL supports it
                          # for reads; writes serialise naturally under WAL)
  reload: false           # optional; default: false — enables hot-reload (development only)
  root_path: ""           # optional; default: "" — set if serving behind a path-prefix proxy
                          # e.g. root_path: "/hippo" for proxy path /hippo/api/v1/...

# ---------------------------------------------------------------------------
# logging (optional)
# ---------------------------------------------------------------------------
logging:
  level: INFO             # optional; default: INFO — "DEBUG" | "INFO" | "WARNING" | "ERROR"
  format: text            # optional; default: text — "text" | "json"
                          # json format produces structured log lines suitable for log aggregators
  file: null              # optional; default: null (stdout only)
                          # when set, logs are written to this file path AND stdout

# ---------------------------------------------------------------------------
# validation (optional)
# Global settings for the validation layer.
# ---------------------------------------------------------------------------
validation:
  max_expand_list_size: 200    # optional; default: 200; hard cap: 1000
                                # Maximum number of list items resolved per `expand: field[]` path.
                                # Validators that would exceed this cap receive a truncated list
                                # and a warning in the log. Set per-validator to override.
                                # See reference_validators_yaml.md §max_expand_list_size.

# ---------------------------------------------------------------------------
# plugins (optional)
# Control plugin discovery and loading.
# ---------------------------------------------------------------------------
plugins:
  disabled: []            # optional; default: [] — list of plugin entry point keys to skip
                          # e.g. disabled: ["my_package.my_validator"] to exclude one plugin
  # Note: all installed plugins in the four entry point groups are loaded by default.
  # Use this only to disable a specific plugin without uninstalling its package.
```

---

### Environment Variable Substitution

String config values support `${VAR_NAME}` and `${VAR_NAME:-default}` substitution:

```yaml
adapter:
  type: postgres
  postgres:
    password: "${MOSAIC_DB_PASSWORD}"           # fails at startup if unset
    host: "${MOSAIC_DB_HOST:-localhost}"        # defaults to "localhost" if unset
```

Substitution is performed at load time before type validation. Substitution is supported
in all string-typed config fields. Non-string fields (int, bool) do not support substitution
in v0.1.

---

### Minimal Configurations

**Smallest possible config (local SQLite):**

```yaml
adapter:
  type: sqlite
  sqlite:
    path: ./mosaic.db
schema:
  path: ./schema.yaml
```

**Local development with validators and debug logging:**

```yaml
adapter:
  type: sqlite
  sqlite:
    path: ./mosaic.db
schema:
  path: ./schema.yaml
validators:
  path: ./validators.yaml
server:
  reload: true
logging:
  level: DEBUG
  format: text
```

**Production-style config (PostgreSQL, structured logging):**

```yaml
adapter:
  type: postgres
  postgres:
    host: "${DB_HOST}"
    database: hippo
    user: hippo
    password: "${DB_PASSWORD}"
    pool_size: 10
    ssl: true
schema:
  path: /etc/hippo/schema.yaml
validators:
  path: /etc/hippo/validators.yaml
server:
  host: 0.0.0.0
  port: 8000
  workers: 4
logging:
  level: INFO
  format: json
```

---

### Config Loading Behaviour

- Config file path defaults to `./mosaic.yaml` in the current working directory
- Override with `MOSAIC_CONFIG=/path/to/mosaic.yaml` environment variable
- `MOSAIC_CONFIG` takes precedence over the default path; it also takes precedence over
  `--config` CLI flag if both are set (CLI flag is the lowest priority)
- All relative paths in the config file are resolved relative to the config file's
  directory, not the current working directory

### `ConfigError` conditions

The config loader raises `ConfigError` (see sec2 §2.15) at startup for:
- Missing required fields (`adapter`, `adapter.type`, `schema.path`)
- Unknown keys at any level
- Invalid enum values (e.g. `adapter.type: mongodb`)
- `max_expand_list_size` exceeding hard cap of 1000
- Schema file not found at declared path
- Validators file not found at declared path (when section is present)
- Environment variable substitution fails (variable unset with no default)

---
