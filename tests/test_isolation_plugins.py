"""Tests for worktree isolation, plugin workflows, and agentType overrides."""

import asyncio
import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Any

import pytest

from openworkflow import MockBackend, ToolAgentBackend, ToolBox, WorkflowRegistry
from openworkflow.runtime import WorkflowRuntime, run_workflow
from openworkflow.worktree import create_worktree, cleanup_worktree, git_root


# ------------------------------------------------------------------ worktree

def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def test_worktree_create_and_cleanup_unchanged(tmp_path):
    _init_repo(tmp_path)
    wt = asyncio.run(create_worktree(str(tmp_path)))
    assert wt and os.path.exists(wt.path)
    assert os.path.exists(os.path.join(wt.path, "f.txt"))  # checked out at HEAD
    kept = asyncio.run(cleanup_worktree(wt))
    assert kept is None  # unchanged -> removed
    assert not os.path.exists(wt.path)


def test_worktree_kept_when_modified(tmp_path):
    _init_repo(tmp_path)
    wt = asyncio.run(create_worktree(str(tmp_path)))
    open(os.path.join(wt.path, "new.txt"), "w").write("x")  # dirty it
    kept = asyncio.run(cleanup_worktree(wt))
    assert kept == wt.path  # modified -> kept


def test_worktree_none_outside_git(tmp_path):
    assert asyncio.run(git_root(str(tmp_path))) is None
    assert asyncio.run(create_worktree(str(tmp_path))) is None


def test_isolation_runs_agent_in_worktree(tmp_path):
    """An agent with isolation:'worktree' runs in the worktree dir, not the repo root."""
    _init_repo(tmp_path)

    # a fake tool-agent client that writes a file then ends — proves it ran in the worktree
    class _Block:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _Resp:
        def __init__(self, content):
            self.content = content
            self.usage = types.SimpleNamespace(output_tokens=3)

    class _Client:
        def __init__(self):
            self._t = 0
            self.messages = self

        async def create(self, **kw):
            self._t += 1
            if self._t == 1:
                return _Resp([_Block(type="tool_use", id="w", name="Write",
                                     input={"file_path": "scratch.txt", "content": "iso"})])
            return _Resp([_Block(type="text", text="done")])

    backend = ToolAgentBackend(client=_Client(), toolbox=ToolBox(cwd=str(tmp_path), confine=False))
    src = (
        "meta = {'name':'iso'}\n"
        "async def main():\n"
        "    return await agent('write scratch', {'isolation': 'worktree'})\n"
    )
    res = asyncio.run(run_workflow(src, backend=backend, quiet=True, cwd=str(tmp_path)))
    # the file was written inside a worktree and that worktree was kept (modified) — so it
    # must NOT be in the repo root
    assert not os.path.exists(os.path.join(str(tmp_path), "scratch.txt"))
    assert "done" in res.result
    assert any("worktree kept" in log for log in res.logs)


# ------------------------------------------------------------------ plugin workflows

def test_plugin_workflows_namespaced(tmp_path, monkeypatch):
    home = tmp_path / "home"
    pdir = home / ".openworkflow" / "plugins" / "myplug" / "workflows"
    pdir.mkdir(parents=True)
    (pdir / "hello.py").write_text(
        "meta = {'name': 'hello', 'description': 'a plugin wf'}\n"
        "async def main():\n    return await agent('hi')\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(home)))

    reg = WorkflowRegistry(cwd=str(tmp_path))
    defs = reg.all()
    assert "myplug:hello" in defs
    assert defs["myplug:hello"].source == "plugin"


# ------------------------------------------------------------------ agentType

def test_agent_type_system_override(tmp_path):
    captured = {}

    class _CaptureBackend(MockBackend):
        async def run(self, prompt, opts):
            captured["system"] = opts.get("system")
            return await super().run(prompt, opts)

    rt = WorkflowRuntime(
        backend=_CaptureBackend(),
        agent_types={"reviewer": "You are a strict code reviewer."},
        progress=__import__("openworkflow.progress", fromlist=["Progress"]).Progress(quiet=True),
    )
    src = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    return await agent('review', {'agentType': 'reviewer'})\n"
    )
    from openworkflow.registry import WorkflowDef
    wdef = WorkflowDef(name="t", description="", source="x", file_path="<i>", script=src, meta={"name": "t"})
    asyncio.run(rt.run(wdef))
    assert captured["system"] == "You are a strict code reviewer."


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
