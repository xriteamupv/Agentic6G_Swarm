"""Model calls of a run (rule 23, SPEC A4 N2), read from its message log before a bench's
temporary directory goes: who called, which function, latency and tokens."""

import json
from pathlib import Path
from typing import Any


def read(comms_path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not comms_path.exists():
        return out
    with comms_path.open() as fh:
        for line in fh:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("direction") != "model":
                continue
            out.append(
                {
                    "agent": m.get("agent"),
                    "role": m.get("binding"),
                    "function": m.get("performative"),
                    "ms": m.get("inference_ms"),
                    "in": m.get("input_tokens"),
                    "out": m.get("output_tokens"),
                    "outcome": m.get("outcome"),
                }
            )
    return out
