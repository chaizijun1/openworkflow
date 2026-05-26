"""Local tool implementations for tool-using subagents.

The original's workflow subagents run with ``tools:["*"]`` — they can read/write files, run
shell, grep, etc., in their own multi-turn loop. This module provides a portable, cwd-scoped
``ToolBox`` with the core file/shell/search tools so our subagents can actually *do work*, not
just generate text.

Each tool exposes an Anthropic-style ``{name, description, input_schema}`` spec and an async
``execute(input) -> str``. Output is truncated to keep tool_results from blowing up context.
File paths are resolved relative to the toolbox ``cwd``; by default writes/reads outside the
working tree are refused (set ``confine=False`` to allow).
"""

from __future__ import annotations

import asyncio
import fnmatch
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

MAX_TOOL_OUTPUT = 30_000  # chars; mirrors the original's per-result cap spirit


def _truncate(s: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…[truncated {len(s) - limit} chars]"


def _html_to_text(html_doc: str) -> str:
    """Very small HTML→text: drop script/style, strip tags, unescape, collapse whitespace."""
    html_doc = re.sub(r"(?is)<(script|style|head|nav|footer)[^>]*>.*?</\1>", " ", html_doc)
    html_doc = re.sub(r"(?i)<br\s*/?>", "\n", html_doc)
    html_doc = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", html_doc)
    text = re.sub(r"<[^>]+>", " ", html_doc)
    text = html.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(ln for ln in lines if ln))


@dataclass
class ToolBox:
    cwd: str = field(default_factory=os.getcwd)
    confine: bool = True          # refuse paths outside cwd
    allow_bash: bool = True       # gate shell access
    bash_timeout: float = 60.0
    allow_web: bool = True        # gate WebFetch/WebSearch
    http_timeout: float = 20.0

    # ------------------------------------------------------------------ path safety
    def _resolve(self, p: str) -> Path:
        path = Path(p)
        if not path.is_absolute():
            path = Path(self.cwd) / path
        path = path.resolve()
        if self.confine:
            root = Path(self.cwd).resolve()
            if root not in path.parents and path != root:
                raise PermissionError(f"path {path} is outside the working tree {root}")
        return path

    # ------------------------------------------------------------------ specs
    def specs(self) -> list[dict]:
        tools = [
            {
                "name": "Read",
                "description": "Read a file from the local filesystem. Returns line-numbered content.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "offset": {"type": "integer", "description": "1-based start line"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["file_path"],
                },
            },
            {
                "name": "Write",
                "description": "Write (overwrite) a file with the given content.",
                "input_schema": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["file_path", "content"],
                },
            },
            {
                "name": "Edit",
                "description": "Replace an exact string in a file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["file_path", "old_string", "new_string"],
                },
            },
            {
                "name": "Grep",
                "description": "Search file contents with a regex. Returns matching lines as path:lineno:text.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                        "glob": {"type": "string", "description": "filter files, e.g. '*.py'"},
                        "ignore_case": {"type": "boolean"},
                    },
                    "required": ["pattern"],
                },
            },
            {
                "name": "Glob",
                "description": "List files matching a glob pattern (recursive with **).",
                "input_schema": {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                    "required": ["pattern"],
                },
            },
        ]
        if self.allow_bash:
            tools.append(
                {
                    "name": "Bash",
                    "description": "Run a shell command in the working directory. Returns stdout+stderr.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "timeout": {"type": "number", "description": "seconds"},
                        },
                        "required": ["command"],
                    },
                }
            )
        if self.allow_web:
            tools += [
                {
                    "name": "WebFetch",
                    "description": "Fetch a URL and return its text content (HTML stripped to text).",
                    "input_schema": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                    },
                },
                {
                    "name": "WebSearch",
                    "description": "Search the web. Returns titles, URLs and snippets. "
                                   "Requires TAVILY_API_KEY or BRAVE_API_KEY.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "max_results": {"type": "integer"},
                        },
                        "required": ["query"],
                    },
                },
            ]
        tools.append(
            {
                "name": "NotebookEdit",
                "description": "Edit a Jupyter notebook (.ipynb) cell: replace, insert, or delete.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "notebook_path": {"type": "string"},
                        "cell_index": {"type": "integer"},
                        "new_source": {"type": "string"},
                        "cell_type": {"type": "string", "enum": ["code", "markdown"]},
                        "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"]},
                    },
                    "required": ["notebook_path"],
                },
            }
        )
        return tools

    # ------------------------------------------------------------------ dispatch
    async def execute(self, name: str, tool_input: dict) -> str:
        try:
            handler = getattr(self, f"_t_{name.lower()}", None)
            if handler is None:
                return f"error: unknown tool '{name}'"
            return _truncate(await handler(tool_input))
        except Exception as e:  # noqa: BLE001 - tool errors flow back to the model as text
            return f"error: {type(e).__name__}: {e}"

    # ------------------------------------------------------------------ tools
    async def _t_read(self, i: dict) -> str:
        path = self._resolve(i["file_path"])
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, (i.get("offset", 1) or 1) - 1)
        end = start + i["limit"] if i.get("limit") else len(lines)
        out = [f"{n + 1}\t{lines[n]}" for n in range(start, min(end, len(lines)))]
        return "\n".join(out) if out else "(empty file)"

    async def _t_write(self, i: dict) -> str:
        path = self._resolve(i["file_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(i["content"], encoding="utf-8")
        return f"wrote {len(i['content'])} chars to {path}"

    async def _t_edit(self, i: dict) -> str:
        path = self._resolve(i["file_path"])
        text = path.read_text(encoding="utf-8")
        old, new = i["old_string"], i["new_string"]
        count = text.count(old)
        if count == 0:
            return f"error: old_string not found in {path}"
        if count > 1 and not i.get("replace_all"):
            return f"error: old_string appears {count}x; pass replace_all or make it unique"
        text = text.replace(old, new) if i.get("replace_all") else text.replace(old, new, 1)
        path.write_text(text, encoding="utf-8")
        return f"edited {path} ({count if i.get('replace_all') else 1} replacement(s))"

    async def _t_glob(self, i: dict) -> str:
        base = self._resolve(i.get("path", ".")) if i.get("path") else Path(self.cwd)
        matches = sorted(str(p) for p in base.glob(i["pattern"]) if p.is_file())
        return "\n".join(matches) if matches else "(no matches)"

    async def _t_grep(self, i: dict) -> str:
        flags = re.IGNORECASE if i.get("ignore_case") else 0
        rx = re.compile(i["pattern"], flags)
        base = self._resolve(i.get("path", ".")) if i.get("path") else Path(self.cwd)
        glob = i.get("glob", "*")
        results: list[str] = []
        files = [base] if base.is_file() else base.rglob("*")
        for f in files:
            if not f.is_file() or not fnmatch.fnmatch(f.name, glob):
                continue
            try:
                for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        results.append(f"{f}:{n}:{line.strip()}")
                        if len(results) >= 200:
                            return "\n".join(results) + "\n…[200 match cap]"
            except OSError:
                continue
        return "\n".join(results) if results else "(no matches)"

    async def _t_bash(self, i: dict) -> str:
        if not self.allow_bash:
            return "error: Bash is disabled for this subagent"
        timeout = float(i.get("timeout") or self.bash_timeout)
        proc = await asyncio.create_subprocess_shell(
            i["command"],
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"error: command timed out after {timeout}s"
        text = out.decode("utf-8", errors="replace")
        return f"(exit {proc.returncode})\n{text}" if text.strip() else f"(exit {proc.returncode}, no output)"

    # ------------------------------------------------------------------ web tools
    async def _t_webfetch(self, i: dict) -> str:
        if not self.allow_web:
            return "error: WebFetch is disabled for this subagent"
        url = i["url"]
        text = await asyncio.to_thread(self._http_get_text, url)
        return text

    def _http_get_text(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "openworkflow/0.1 (+webfetch)"})
        with urllib.request.urlopen(req, timeout=self.http_timeout) as resp:  # noqa: S310
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read()
        body = raw.decode("utf-8", errors="replace")
        if "html" in ctype or body.lstrip()[:1] == "<":
            body = _html_to_text(body)
        return body

    async def _t_websearch(self, i: dict) -> str:
        if not self.allow_web:
            return "error: WebSearch is disabled for this subagent"
        query = i["query"]
        n = int(i.get("max_results") or 5)
        return await asyncio.to_thread(self._search, query, n)

    def _search(self, query: str, n: int) -> str:
        if os.environ.get("TAVILY_API_KEY"):
            return self._search_tavily(query, n)
        if os.environ.get("BRAVE_API_KEY"):
            return self._search_brave(query, n)
        return ("error: WebSearch not configured — set TAVILY_API_KEY or BRAVE_API_KEY "
                "(or use WebFetch/Bash curl for specific URLs)")

    def _search_tavily(self, query: str, n: int) -> str:
        payload = json.dumps({"api_key": os.environ["TAVILY_API_KEY"], "query": query,
                              "max_results": n}).encode()
        req = urllib.request.Request("https://api.tavily.com/search", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.http_timeout) as resp:  # noqa: S310
            data = json.loads(resp.read())
        rows = [f"- {r.get('title')} — {r.get('url')}\n  {r.get('content', '')[:200]}"
                for r in data.get("results", [])]
        return "\n".join(rows) if rows else "(no results)"

    def _search_brave(self, query: str, n: int) -> str:
        qs = urllib.parse.urlencode({"q": query, "count": n})
        req = urllib.request.Request(
            f"https://api.search.brave.com/res/v1/web/search?{qs}",
            headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"],
                     "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.http_timeout) as resp:  # noqa: S310
            data = json.loads(resp.read())
        rows = [f"- {r.get('title')} — {r.get('url')}\n  {r.get('description', '')[:200]}"
                for r in data.get("web", {}).get("results", [])]
        return "\n".join(rows) if rows else "(no results)"

    # ------------------------------------------------------------------ notebook
    async def _t_notebookedit(self, i: dict) -> str:
        path = self._resolve(i["notebook_path"])
        nb = json.loads(path.read_text(encoding="utf-8"))
        cells = nb.setdefault("cells", [])
        mode = i.get("edit_mode", "replace")
        idx = i.get("cell_index", 0)
        if mode == "delete":
            if not (0 <= idx < len(cells)):
                return f"error: cell_index {idx} out of range"
            cells.pop(idx)
        else:
            src = i.get("new_source", "")
            cell = {"cell_type": i.get("cell_type", "code"), "metadata": {},
                    "source": src.splitlines(keepends=True)}
            if i.get("cell_type", "code") == "code":
                cell["outputs"] = []
                cell["execution_count"] = None
            if mode == "insert":
                cells.insert(idx, cell)
            else:  # replace
                if not (0 <= idx < len(cells)):
                    return f"error: cell_index {idx} out of range"
                cells[idx] = cell
        path.write_text(json.dumps(nb, indent=1), encoding="utf-8")
        return f"{mode} cell {idx} in {path} ({len(cells)} cells total)"
