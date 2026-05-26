"""Tests for the expanded tool registry: WebFetch, WebSearch, NotebookEdit, and MCP wiring."""

import asyncio
import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from openworkflow import MCPManager, ToolAgentBackend, ToolBox


# ----------------------------------------------------------------- WebFetch

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><head><style>x{}</style></head><body>"
                         b"<h1>Title</h1><p>Hello &amp; welcome</p>"
                         b"<script>evil()</script></body></html>")

    def log_message(self, *a):
        pass


@pytest.fixture
def http_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def test_webfetch_strips_html(http_server):
    tb = ToolBox()
    out = asyncio.run(tb.execute("WebFetch", {"url": http_server}))
    assert "Title" in out
    assert "Hello & welcome" in out      # entity unescaped
    assert "evil()" not in out           # script stripped
    assert "<h1>" not in out             # tags stripped


def test_webfetch_disabled():
    tb = ToolBox(allow_web=False)
    assert "WebFetch" not in [s["name"] for s in tb.specs()]
    out = asyncio.run(tb.execute("WebFetch", {"url": "http://x"}))
    assert out.startswith("error: WebFetch is disabled")


def test_websearch_not_configured(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    out = asyncio.run(ToolBox().execute("WebSearch", {"query": "x"}))
    assert "not configured" in out


# ----------------------------------------------------------------- NotebookEdit

def test_notebook_edit_replace_insert_delete(tmp_path):
    nb = tmp_path / "n.ipynb"
    nb.write_text(json.dumps({"cells": [
        {"cell_type": "code", "source": ["print(1)"], "outputs": [], "execution_count": None}
    ], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
    tb = ToolBox(cwd=str(tmp_path))

    asyncio.run(tb.execute("NotebookEdit", {"notebook_path": "n.ipynb", "cell_index": 0,
                                            "new_source": "print(2)"}))
    cells = json.loads(nb.read_text())["cells"]
    assert "print(2)" in "".join(cells[0]["source"])

    asyncio.run(tb.execute("NotebookEdit", {"notebook_path": "n.ipynb", "cell_index": 1,
                                            "new_source": "# md", "cell_type": "markdown",
                                            "edit_mode": "insert"}))
    cells = json.loads(nb.read_text())["cells"]
    assert len(cells) == 2 and cells[1]["cell_type"] == "markdown"

    asyncio.run(tb.execute("NotebookEdit", {"notebook_path": "n.ipynb", "cell_index": 0,
                                            "edit_mode": "delete"}))
    assert len(json.loads(nb.read_text())["cells"]) == 1


# ----------------------------------------------------------------- MCP wiring

class _FakeMCP(MCPManager):
    """A connected MCP manager exposing one fake tool, no real SDK/servers."""

    def __init__(self):
        super().__init__(config={"demo": {"command": "x"}})
        self._connected = True
        self._tools = {"mcp__demo__echo": ("demo", "echo")}
        self._specs = [{"name": "mcp__demo__echo", "description": "echo back",
                        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}}}]
        self.executed = []

    async def connect(self):
        self._connected = True

    async def execute(self, name, tool_input):
        self.executed.append((name, tool_input))
        return f"echoed: {tool_input.get('text')}"


def test_mcp_specs_merged_and_dispatched():
    mcp = _FakeMCP()

    class _Block:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _Resp:
        def __init__(self, content):
            self.content = content
            self.usage = types.SimpleNamespace(output_tokens=4)

    class _Client:
        def __init__(self):
            self._t = 0
            self.messages = self
            self.seen_tools = None

        async def create(self, **kw):
            self._t += 1
            if self._t == 1:
                self.seen_tools = [t["name"] for t in kw["tools"]]
                return _Resp([_Block(type="tool_use", id="m1", name="mcp__demo__echo",
                                     input={"text": "hi"})])
            return _Resp([_Block(type="text", text="done")])

    client = _Client()
    backend = ToolAgentBackend(client=client, toolbox=ToolBox(allow_bash=False, allow_web=False),
                               mcp=mcp)
    res = asyncio.run(backend.run("use the echo tool", {}))
    # MCP tool was advertised to the model and routed to the manager
    assert "mcp__demo__echo" in client.seen_tools
    assert mcp.executed == [("mcp__demo__echo", {"text": "hi"})]
    assert "done" in res.value


def test_mcp_manager_no_config_is_noop():
    mgr = MCPManager(config={})
    assert not mgr.configured()
    asyncio.run(mgr.connect())
    assert mgr.tool_specs() == []
    assert not mgr.handles("mcp__x__y")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
