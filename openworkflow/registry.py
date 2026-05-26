"""Saved-workflow registry.

Mirrors the original's three sources (user / project / built-in) with the same precedence:
user overrides project overrides built-in when names collide.

  * user      : ~/.openworkflow/workflows/*.py        (cross-repo personal workflows)
  * project   : ./.openworkflow/workflows/*.py         (team-shared, would be checked into VCS)
  * built-in  : <package>/../workflows/*.py           (ships with openworkflow)

Each file is a workflow script (first statement ``meta = {...}``); its ``meta["name"]`` is the
registry key, used by ``Workflow({name: ...})`` / the inline ``workflow(name)`` primitive.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .sandbox import WorkflowScriptError, compile_script

# Size cap mirroring the original's per-file byte limit (skip oversized files).
MAX_SCRIPT_BYTES = 256 * 1024


@dataclass
class WorkflowDef:
    name: str
    description: str
    source: str  # "user" | "project" | "built-in" | "scriptPath"
    file_path: str
    script: str
    meta: dict


def _builtin_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "workflows"


def _user_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".openworkflow" / "workflows"


def _project_dir(cwd: str | None = None) -> Path:
    return Path(cwd or os.getcwd()) / ".openworkflow" / "workflows"


def _plugins_root() -> Path:
    # each subdir is a plugin; its workflows live in <plugin>/workflows/*.py
    return Path(os.path.expanduser("~")) / ".openworkflow" / "plugins"


class WorkflowRegistry:
    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd or os.getcwd()
        self._cache: dict[str, WorkflowDef] | None = None

    def _scan(self, directory: Path, source: str, name_prefix: str = "") -> list[WorkflowDef]:
        defs: list[WorkflowDef] = []
        if not directory.is_dir():
            return defs
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                if path.stat().st_size > MAX_SCRIPT_BYTES:
                    continue
                src = path.read_text(encoding="utf-8")
                compiled = compile_script(src, filename=str(path))
            except (OSError, WorkflowScriptError):
                continue  # invalid meta / unreadable -> skip, like the original
            meta = compiled.meta
            defs.append(
                WorkflowDef(
                    name=name_prefix + meta["name"],
                    description=meta.get("description", ""),
                    source=source,
                    file_path=str(path),
                    script=src,
                    meta=meta,
                )
            )
        return defs

    def _scan_plugins(self) -> list[WorkflowDef]:
        root = _plugins_root()
        defs: list[WorkflowDef] = []
        if not root.is_dir():
            return defs
        for plugin_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            wf_dir = plugin_dir / "workflows"
            # plugin workflows are namespaced "<plugin>:<name>", like the original
            defs.extend(self._scan(wf_dir, "plugin", name_prefix=f"{plugin_dir.name}:"))
        return defs

    def all(self) -> dict[str, WorkflowDef]:
        if self._cache is not None:
            return self._cache
        merged: dict[str, WorkflowDef] = {}
        # lowest precedence first so later sources overwrite on name collision:
        # built-in < plugin < project < user
        for d in self._scan(_builtin_dir(), "built-in"):
            merged[d.name] = d
        for d in self._scan_plugins():
            merged[d.name] = d
        for directory, source in (
            (_project_dir(self.cwd), "project"),
            (_user_dir(), "user"),
        ):
            for d in self._scan(directory, source):
                merged[d.name] = d
        self._cache = merged
        return merged

    def get(self, name: str) -> WorkflowDef | None:
        return self.all().get(name)

    def from_script_path(self, path: str) -> WorkflowDef:
        p = Path(path)
        src = p.read_text(encoding="utf-8")
        compiled = compile_script(src, filename=str(p))
        return WorkflowDef(
            name=compiled.meta["name"],
            description=compiled.meta.get("description", ""),
            source="scriptPath",
            file_path=str(p),
            script=src,
            meta=compiled.meta,
        )
