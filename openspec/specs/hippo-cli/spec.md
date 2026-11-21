# hippo-cli Specification

## Purpose
TBD - created by archiving change tui-browser-schema-explorer. Update Purpose after archive.
## Requirements
### Requirement: mosaic tui subcommand launches the TUI application
The system SHALL add a `tui` Click command to `src/mosaic/cli/main.py`. The command SHALL
accept the following options: `--backend` (choices: `sdk`, `rest`; default: `sdk`),
`--url` (string; default: `http://127.0.0.1:8000`; REST mode only),
`--token` (string; default: `dev-token`; REST mode only),
`--db` (path; SDK mode only). The command SHALL instantiate the appropriate backend via the
backend factory and launch `HippoTUIApp`.

#### Scenario: mosaic tui launches in SDK mode by default
- **WHEN** the user runs `mosaic tui` with no flags
- **THEN** the TUI starts using `SDKBackend` with `db_path` resolved from `config.json`

#### Scenario: mosaic tui --backend rest connects to running server
- **WHEN** the user runs `mosaic tui --backend rest --url http://host:8000 --token t`
- **THEN** the TUI starts using `RESTBackend` pointed at `http://host:8000` with token `t`

#### Scenario: mosaic tui --db overrides config.json db path in sdk mode
- **WHEN** the user runs `mosaic tui --db /data/custom.db`
- **THEN** `SDKBackend` uses `/data/custom.db` as the SQLite path

#### Scenario: mosaic tui --help is available without Textual installed
- **WHEN** the user runs `mosaic tui --help` and Textual is not installed
- **THEN** Click displays the help text without importing `HippoTUIApp`

---

### Requirement: Missing Textual dependency produces a clear install error
The system SHALL guard the import of `src/mosaic/tui` behind a `try/except ImportError`
block in the `tui` Click command handler. If `textual` is not importable, the command SHALL
raise a `click.ClickException` with the message:
`"TUI requires 'pip install hippo[tui]'"`.

#### Scenario: Running mosaic tui without textual shows install guidance
- **WHEN** Textual is not installed and the user runs `mosaic tui`
- **THEN** the CLI prints `Error: TUI requires 'pip install hippo[tui]'` and exits with a non-zero code

#### Scenario: Running mosaic tui with textual installed starts normally
- **WHEN** Textual is installed and the user runs `mosaic tui`
- **THEN** the TUI application launches without error

---

### Requirement: Textual and httpx are added as optional [tui] dependencies
The system SHALL declare `textual>=0.50.0,<1.0` and `httpx>=0.27.0` as optional
dependencies in the `[tui]` extras group in `pyproject.toml`. These packages SHALL NOT be
installed as part of the default `pip install hippo` invocation.

#### Scenario: pip install hippo does not install textual
- **WHEN** the user runs `pip install hippo` without extras
- **THEN** `textual` and `httpx` are not present in the environment

#### Scenario: pip install hippo[tui] installs both optional dependencies
- **WHEN** the user runs `pip install hippo[tui]`
- **THEN** both `textual` and `httpx` are installed at compatible versions

