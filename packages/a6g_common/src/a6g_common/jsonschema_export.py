"""JSON Schema export for partners (``a6g schemas export``)."""

import json
from pathlib import Path
from typing import Any

from a6g_common.schemas import EXPORTED_SCHEMAS, SCHEMA_VERSION

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID_BASE = "https://agentic6g.eu/uc2/a6g-swarm/schemas"


def json_schema_for(name: str) -> dict[str, Any]:
    """Return the JSON Schema (draft 2020-12) of one exported model."""
    schema = EXPORTED_SCHEMAS[name].model_json_schema(mode="validation")
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": f"{SCHEMA_ID_BASE}/{SCHEMA_VERSION}/{name}.schema.json",
        **schema,
    }


def export_json_schemas(out_dir: Path) -> list[Path]:
    """Write one ``<Name>.schema.json`` per exported model; return the written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in sorted(EXPORTED_SCHEMAS):
        path = out_dir / f"{name}.schema.json"
        path.write_text(json.dumps(json_schema_for(name), indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written
