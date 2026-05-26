"""Tests for the ToolBox and the tool-using subagent loop (no API key needed)."""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from openworkflow import ToolAgentBackend, ToolBox


# ------------------------------------------------------------------ ToolBox

def test_write_then_read(tmp_path):
    tb = ToolBox(cwd=str(tmp_path))
    asyncio.run(tb.execute("Write", {"file_path": "a.txt", "content": "hello\nworld"}))
    out = asyncio.run(tb.execute("Read", {"file_path": "a.txt"}))
    assert "1\thello" in out and "2\tworld" in out


def test_edit(tmp_path):
    tb = ToolBox(cwd=str(tmp_path))
    asyncio.run(tb.execute("Write", {"file_path": "a.txt", "content": "foo bar"}))
    asyncio.run(tb.execute("Edit", {"file_path": "a.txt", "old_string": "bar", "new_string": "baz"}))
    out = asyncio.run(tb.execute("Read", {"file_path": "a.txt"}))
    assert "foo baz" in out


def test_grep_and_glob(tmp_path):
    tb = ToolBox(cwd=str(tmp_path))
    asyncio.run(tb.execute("Write", {"file_path": "x.py", "content": "def foo():\n    return 1"}))
    asyncio.run(tb.execute("Write", {"file_path": "y.py", "content": "x = 2"}))
    g = asyncio.run(tb.execute("Grep", {"pattern": "def ", "glob": "*.py"}))
    assert "x.py" in g and "def foo" in g
    gl = asyncio.run(tb.execute("Glob", {"pattern": "*.py"}))
    assert "x.py" in gl and "y.py" in gl


def test_bash(tmp_path):
    tb = ToolBox(cwd=str(tmp_path))
    out = asyncio.run(tb.execute("Bash", {"command": "echo hi"}))
    assert "hi" in out and "exit 0" in out


def test_confinement_refuses_outside_path(tmp_path):
    tb = ToolBox(cwd=str(tmp_path), confine=True)
    out = asyncio.run(tb.execute("Read", {"file_path": "/etc/hosts"}))
    assert out.startswith("error:")


def test_bash_disabled():
    specs = ToolBox(allow_bash=False).specs()
    assert not any(s["name"] == "Bash" for s in specs)


# ------------------------------------------------------------------ tool-use loop

class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content):
        self.content = content
        self.usage = types.SimpleNamespace(output_tokens=5)


class _FakeClient:
    """Scripts a sequence of model responses: first a tool_use, then a final text answer."""

    def __init__(self, file_path):
        self.file_path = file_path
        self._turn = 0
        self.messages = self  # so client.messages.create works

    async def create(self, **kw):
        self._turn += 1
        if self._turn == 1:
            return _Resp([
                _Block(type="text", text="I'll write the file."),
                _Block(type="tool_use", id="t1", name="Write",
                       input={"file_path": self.file_path, "content": "done"}),
            ])
        return _Resp([_Block(type="text", text="Wrote the file successfully.")])


def test_tool_agent_loop_executes_tools(tmp_path):
    target = str(tmp_path / "out.txt")
    backend = ToolAgentBackend(
        client=_FakeClient(target),
        toolbox=ToolBox(cwd=str(tmp_path)),
    )
    res = asyncio.run(backend.run("create out.txt", {}))
    # the loop actually executed the Write tool, then returned the final text
    assert os.path.exists(target)
    assert open(target).read() == "done"
    assert "successfully" in res.value
    assert res.output_tokens == 10  # 5 per turn x 2 turns


class _SchemaClient:
    def __init__(self):
        self._turn = 0
        self.messages = self

    async def create(self, **kw):
        self._turn += 1
        if self._turn == 1:
            return _Resp([_Block(type="tool_use", id="g1", name="Grep",
                                 input={"pattern": "x"})])
        return _Resp([_Block(type="tool_use", id="s1", name="StructuredOutput",
                             input={"answer": 42})])


def test_tool_agent_schema_via_structured_output(tmp_path):
    backend = ToolAgentBackend(client=_SchemaClient(), toolbox=ToolBox(cwd=str(tmp_path)))
    res = asyncio.run(backend.run("find x", {"schema": {"type": "object",
                                                        "properties": {"answer": {"type": "integer"}}}}))
    assert res.value == {"answer": 42}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
