"""Git worktree isolation for ``agent(..., {"isolation": "worktree"})``.

Mirrors the original: an agent run with worktree isolation gets a fresh git worktree (detached
at HEAD) so parallel agents mutating files don't clobber each other. After the agent finishes,
an *unchanged* worktree is auto-removed; a *modified* one is kept and its path reported (so the
work isn't lost). If the cwd isn't a git repo, isolation degrades gracefully to a no-op.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass


async def _git(args: list[str], cwd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def git_root(cwd: str) -> str | None:
    try:
        code, out, _ = await _git(["rev-parse", "--show-toplevel"], cwd)
    except FileNotFoundError:  # git not installed
        return None
    return out.strip() if code == 0 and out.strip() else None


@dataclass
class Worktree:
    path: str   # the isolated working dir to run the agent in
    root: str   # the source repo root
    tmp: str    # temp dir to remove on cleanup


async def create_worktree(cwd: str) -> Worktree | None:
    """Create a detached worktree at HEAD; returns None if cwd isn't a git repo."""
    root = await git_root(cwd)
    if not root:
        return None
    tmp = tempfile.mkdtemp(prefix="owf-wt-")
    wt_path = f"{tmp}/wt"
    code, _out, err = await _git(["worktree", "add", "--detach", wt_path, "HEAD"], root)
    if code != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"git worktree add failed: {err.strip()}")
    return Worktree(path=wt_path, root=root, tmp=tmp)


async def worktree_dirty(wt: Worktree) -> bool:
    code, out, _ = await _git(["status", "--porcelain"], wt.path)
    return code == 0 and bool(out.strip())


async def cleanup_worktree(wt: Worktree) -> str | None:
    """Remove the worktree if unchanged; otherwise keep it. Returns the kept path or None."""
    if await worktree_dirty(wt):
        return wt.path  # keep modified work
    await _git(["worktree", "remove", "--force", wt.path], wt.root)
    shutil.rmtree(wt.tmp, ignore_errors=True)
    return None
