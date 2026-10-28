"""``a6g dev reset``: clear the development stack's accumulated state.

``langgraph dev`` keeps every thread, run and checkpoint in memory and in pickles under
``.langgraph_api/`` and never expires them (thread TTL is not implemented by its in-memory
runtime). Every robot report is a run on the mission thread, so after long sessions the
server needs gigabytes and stops picking up runs. The simulation stacks keep the robots'
checkpoints and the comms/decision logs under ``var/sim_stack`` / ``var/gz_stack``.

Only while nothing is running (the files are rewritten by the live processes): the parts
are moved to ``var/reset/<stamp>/`` (or deleted) and the stacks start fresh.
"""

import shutil
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

#: What each part holds on disk (relative to the repository root).
PARTS: dict[str, tuple[str, ...]] = {
    "server": (".langgraph_api", "var/decisions.jsonl"),  # threads/runs; coordinator log
    "sim": ("var/sim_stack", "var/gz_stack"),
}
#: Who must be stopped first: the ports the running processes listen on.
PROCESSES: dict[str, tuple[tuple[int, str], ...]] = {
    "server": ((2024, "langgraph dev (terminal 2)"),),
    "sim": (
        (8300, "a6g-sim-stack / a6g-gz-stack (terminal 1)"),
        (8410, "the interaction store of the sim stack (terminal 1)"),
    ),
}
BACKUPS = "var/reset"


@dataclass(frozen=True)
class Item:
    path: Path
    bytes: int


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def find_root(start: Path | None = None) -> Path:
    """The repository root: the nearest directory with ``langgraph.json``."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "langgraph.json").is_file():
            return d
    raise FileNotFoundError("no langgraph.json here or above: run it inside a6g-swarm")


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def running(parts: list[str], busy: Callable[[int], bool] | None = None) -> list[str]:
    """The processes still running that hold these parts."""
    busy = busy or port_in_use
    return [who for part in parts for port, who in PROCESSES[part] if busy(port)]


def plan(root: Path, parts: list[str]) -> list[Item]:
    paths = [root / rel for part in parts for rel in PARTS[part]]
    return [Item(p, size_of(p)) for p in paths if p.exists()]


def reset(root: Path, items: list[Item], delete: bool, stamp: str | None = None) -> Path | None:
    """Move the items to ``var/reset/<stamp>/`` (returned), or delete them."""
    if delete:
        for item in items:
            if item.path.is_dir():
                shutil.rmtree(item.path)
            else:
                item.path.unlink()
        return None
    dest = root / BACKUPS / (stamp or datetime.now().strftime("%Y%m%d-%H%M%S"))
    dest.mkdir(parents=True, exist_ok=False)
    for item in items:
        item.path.rename(dest / item.path.relative_to(root).as_posix().replace("/", "__"))
    return dest


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
