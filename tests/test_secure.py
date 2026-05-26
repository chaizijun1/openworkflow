"""Tests for the secure (OS-sandboxed) runtime.

End-to-end tests are skipped when no OS sandbox is available (non-macOS without bwrap), but the
framing and profile-builder tests always run.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from openworkflow import MockBackend, run_workflow_secure, sandbox_available
from openworkflow._rpc import pack, read_msg
from openworkflow.runtime import BudgetExhausted
from openworkflow.seatbelt import SandboxSpec, build_seatbelt_profile

requires_sandbox = pytest.mark.skipif(not sandbox_available(), reason="no OS sandbox available")


def secure(src, **kw):
    return asyncio.run(run_workflow_secure(src, backend=MockBackend(), quiet=True, **kw))


# ----------------------------------------------------------------- framing

def test_framing_roundtrip():
    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(pack({"hello": "世界", "n": 1}))
        reader.feed_data(pack({"second": True}))
        reader.feed_eof()
        a = await read_msg(reader)
        b = await read_msg(reader)
        c = await read_msg(reader)
        return a, b, c
    a, b, c = asyncio.run(go())
    assert a == {"hello": "世界", "n": 1}
    assert b == {"second": True}
    assert c is None  # clean EOF


# ----------------------------------------------------------------- profile builder

def test_profile_denies_network_and_writes(tmp_path):
    prof = build_seatbelt_profile(SandboxSpec(scratch=str(tmp_path)))
    assert "(deny default)" in prof
    assert "(deny network*)" in prof
    assert "(allow file-read*)" in prof
    # scratch is writable, resolved to its real path
    assert os.path.realpath(str(tmp_path)) in prof


def test_profile_denies_secret_dirs(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(fake_home)))
    prof = build_seatbelt_profile(
        SandboxSpec(scratch=str(tmp_path), deny_read=["~/.ssh"])
    )
    assert "(deny file-read*" in prof
    assert os.path.realpath(str(fake_home / ".ssh")) in prof


# ----------------------------------------------------------------- end-to-end

@requires_sandbox
def test_secure_run_orchestrates():
    src = (
        "meta = {'name':'t', 'phases':['p']}\n"
        "async def main():\n"
        "    phase('p')\n"
        "    log('hi from sandbox')\n"
        "    parts = await parallel([lambda i=i: agent(f't{i}', {'phase':'p'}) for i in range(3)])\n"
        "    return len([p for p in parts if p])\n"
    )
    res = secure(src)
    assert res.result == 3
    assert res.agent_count == 3
    assert "hi from sandbox" in res.logs


@requires_sandbox
def test_secure_blocks_network_and_filesystem(tmp_path):
    marker = tmp_path / "should_not_exist.txt"
    src = (
        "meta = {'name':'evil'}\n"
        "async def main():\n"
        "    out = {}\n"
        "    try:\n"
        "        import socket; socket.create_connection(('1.1.1.1',80),timeout=3)\n"
        "        out['net'] = 'LEAK'\n"
        "    except Exception as e:\n"
        "        out['net'] = type(e).__name__\n"
        "    try:\n"
        f"        open({str(marker)!r}, 'w').write('x')\n"
        "        out['write'] = 'LEAK'\n"
        "    except Exception as e:\n"
        "        out['write'] = type(e).__name__\n"
        "    return out\n"
    )
    res = secure(src)
    assert res.result["net"] != "LEAK"
    assert res.result["write"] != "LEAK"
    assert not marker.exists()  # the write really did not happen


@requires_sandbox
def test_secure_budget_ceiling():
    src = (
        "meta = {'name':'t'}\n"
        "async def main():\n"
        "    out = []\n"
        "    for i in range(50):\n"
        "        out.append(await agent('prompt number ' + str(i)))\n"
        "    return out\n"
    )
    with pytest.raises(RuntimeError):  # BudgetExhausted surfaces as a failed sandboxed run
        secure(src, budget_total=5)


@requires_sandbox
def test_secure_nested_workflow(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(
        "meta = {'name':'child'}\nasync def main():\n    return await agent('child work')\n"
    )
    src = (
        "meta = {'name':'parent'}\n"
        "async def main():\n"
        "    return await workflow({'scriptPath': %r})\n" % str(child)
    )
    res = secure(src)
    assert "child work" in res.result
    assert res.agent_count == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
