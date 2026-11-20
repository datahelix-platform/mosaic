"""Template registry and loaders for mosaic init."""

from dataclasses import dataclass
from importlib import resources
from typing import Any


TEMPLATES = {
    "bibliography": {
        "name": "Bibliography",
        "description": (
            "Domain-neutral citation-graph schema demonstrating Mosaic's LinkML "
            "runtime features (default)"
        ),
        "files": {
            "schema.yaml": "bibliography_schema",
            "config.json": "bibliography_config",
            "README.md": "bibliography_readme",
            ".gitignore": "basic_gitignore",
        },
    },
    "basic": {
        "name": "Basic",
        "description": "Minimal Mosaic project with core configuration",
        "files": {
            "config.json": "basic_config",
            ".gitignore": "basic_gitignore",
        },
    },
    "minimal": {
        "name": "Minimal",
        "description": "Lightweight setup with just config.json",
        "files": {
            "config.json": "minimal_config",
            ".gitignore": "minimal_gitignore",
        },
    },
    "full": {
        "name": "Full",
        "description": "Complete project with config, schemas, and sample data",
        "files": {
            "config.json": "full_config",
            "README.md": "full_readme",
            ".gitignore": "full_gitignore",
            "schemas/.keep": "empty",
        },
    },
}


@dataclass
class Template:
    name: str
    description: str
    files: dict[str, str]


def get_template(name: str) -> Template | None:
    """Get a template by name."""
    template_data = TEMPLATES.get(name)
    if template_data is None:
        return None
    return Template(
        name=name,
        description=template_data["description"],
        files=template_data["files"],
    )


def list_templates() -> list[dict[str, str]]:
    """List all available templates."""
    return [
        {"name": name, "description": data["description"]}
        for name, data in TEMPLATES.items()
    ]


def _load_bundled_resource(filename: str) -> str:
    """Read a file packaged under mosaic.cli.template_data."""
    return (
        resources.files("mosaic.cli.template_data")
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


def get_template_file_content(file_key: str) -> str | None:
    """Get the content for a template file by key."""
    if file_key == "bibliography_schema":
        return _load_bundled_resource("bibliography.yaml")

    contents = {
        "bibliography_config": """{
  "schema_path": "schema.yaml",
  "storage_backend": "sqlite",
  "database_url": "data/mosaic.db",
  "validation_enabled": true
}
""",
        "bibliography_readme": """# Mosaic Bibliography Project

This project was scaffolded by `mosaic init` using the **bibliography**
template — a domain-neutral citation graph (Author, Publication, Venue,
Citation) that demonstrates Mosaic's core LinkML runtime features:

- Polymorphic entity hierarchies (Publication → JournalArticle / Preprint /
  ConferencePaper)
- Full-text search (`mosaic_search: fts5`) on titles, abstracts, journal names
- B-tree indexes (`mosaic_index: true`) on FK-style slots
- Entity supersession (preprint → journal article)

## Quick start

```bash
# Apply the schema to the local SQLite database
mosaic migrate

# Start the REST API server
mosaic serve

# Validate a data bundle
mosaic validate --schema schema.yaml --data path/to/bundle.yaml
```

## Project layout

- `schema.yaml` — LinkML schema (edit to model your own domain)
- `mosaic.yaml` — Mosaic runtime config
- `config.json` — legacy project config
- `data/` — runtime data and SQLite database
""",
        "basic_config": """{
  "version": "0.1",
  "storage": {
    "type": "sqlite",
    "path": "mosaic.db"
  },
  "schema": {
    "type": "linkml"
  }
}
""",
        "minimal_config": """{
  "version": "0.1",
  "storage": {
    "type": "sqlite",
    "path": "mosaic.db"
  }
}
""",
        "full_config": """{
  "version": "0.1",
  "name": "mosaic-project",
  "description": "Mosaic Metadata Tracking Service Project",
  "storage": {
    "type": "sqlite",
    "path": "mosaic.db"
  },
  "schema": {
    "type": "linkml",
    "path": "schemas"
  },
  "validation": {
    "enabled": true,
    "strict": false
  },
  "api": {
    "host": "127.0.0.1",
    "port": 8000
  }
}
""",
        "basic_gitignore": """# Mosaic
*.db
*.db-journal
*.log

# Python
__pycache__/
*.py[cod]
*$py.class
*.egg-info/
dist/
build/

# IDE
.vscode/
.idea/

# OS
.DS_Store
Thumbs.db
""",
        "minimal_gitignore": """# Mosaic
*.db
""",
        "full_gitignore": """# Mosaic
*.db
*.db-journal
*.log

# Python
__pycache__/
*.py[cod]
*$py.class
*.egg-info/
dist/
build/
.eggs/

# Virtual environments
venv/
env/
.venv/

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# Data
data/*.tmp
""",
        "full_readme": """# Mosaic Project

This is a Mosaic Metadata Tracking Service project.

## Quick Start

```bash
# Start the server
mosaic serve

# Validate schemas
mosaic validate

# Ingest data
mosaic ingest data/sample.csv --entity-type sample
```

## Project Structure

- `config.json` - Project configuration
- `schemas/` - LinkML schema files
- `data/` - Data files for ingestion
""",
        "empty": "",
    }
    return contents.get(file_key)
