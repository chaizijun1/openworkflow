"""OS sandbox launchers — the kernel-enforced boundary for the workflow script.

Two backends, selected by platform:

  * macOS: ``sandbox-exec`` with a deny-default Seatbelt profile (ships with the OS, kernel
    enforced, no install). This is what Claude Code / Cursor / OpenClaw use on macOS.
  * Linux: ``bubblewrap`` (``bwrap``) with namespaces + ``--unshare-net`` (if installed).

Default posture (both): **no network**, **no filesystem writes outside a scratch dir**, reads
allowed (so Python can start), and common secret directories explicitly denied for read. Even if
the untrusted script reads a secret, with egress blocked it cannot exfiltrate it, and it cannot
persist anything outside scratch. Its only channel out is the RPC pipe to the trusted host.

The script *cannot opt out*: a Seatbelt profile inherits to children and can't be removed from
inside; bwrap namespaces likewise.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field

# Directories whose *read* we deny by default (defense-in-depth; egress is already blocked).
DEFAULT_DENY_READ = [
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.gnupg",
    "~/.kube",
    "~/.docker",
    "~/.netrc",
    "~/.config/openworkflow",  # in case anyone stores keys there
]


def _real(path: str) -> str:
    """Resolve symlinks — Seatbelt matches real paths (/var -> /private/var on macOS)."""
    return os.path.realpath(os.path.expanduser(path))


@dataclass
class SandboxSpec:
    scratch: str                      # the only writable dir
    deny_read: list[str] = field(default_factory=lambda: list(DEFAULT_DENY_READ))
    allow_read_roots: list[str] | None = None  # if set, ONLY these (+system) are readable


def build_seatbelt_profile(spec: SandboxSpec) -> str:
    scratch = _real(spec.scratch)
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm)",
        "(allow signal (target self))",
    ]
    if spec.allow_read_roots:
        # strict allowlist: only named roots plus the dirs Python needs to boot
        roots = spec.allow_read_roots + [sys.prefix, sys.base_prefix, "/usr", "/System",
                                         "/Library", "/private/var/db/dyld", "/dev", scratch]
        for r in roots:
            lines.append(f'(allow file-read* (subpath "{_real(r)}"))')
    else:
        lines.append("(allow file-read*)")
    # writes: only scratch + the null/zero/random devices
    lines.append(f'(allow file-write* (subpath "{scratch}"))')
    lines.append('(allow file-write-data (literal "/dev/null") (literal "/dev/zero") '
                 '(literal "/dev/random") (literal "/dev/urandom"))')
    # explicit read denials (override the broad allow — last match wins in SBPL)
    for d in spec.deny_read:
        rp = _real(d)
        if os.path.exists(rp):
            lines.append(f'(deny file-read* (subpath "{rp}"))')
    lines.append("(deny network*)")
    return "\n".join(lines)


def have_seatbelt() -> bool:
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def have_bwrap() -> bool:
    return sys.platform.startswith("linux") and shutil.which("bwrap") is not None


def sandbox_command(spec: SandboxSpec, inner: list[str]) -> list[str]:
    """Wrap ``inner`` (the python child command) in the platform sandbox launcher."""
    if have_seatbelt():
        profile = build_seatbelt_profile(spec)
        return ["sandbox-exec", "-p", profile, *inner]
    if have_bwrap():
        scratch = _real(spec.scratch)
        cmd = [
            "bwrap",
            "--unshare-net",          # no network namespace -> no egress
            "--ro-bind", "/", "/",    # read-only root
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--bind", scratch, scratch,  # writable scratch
        ]
        for d in spec.deny_read:
            rp = _real(d)
            if os.path.exists(rp):
                cmd += ["--tmpfs", rp]  # mask secret dirs with empty tmpfs
        return [*cmd, *inner]
    raise RuntimeError(
        "no OS sandbox available: need sandbox-exec (macOS) or bwrap (Linux). "
        "Install bubblewrap on Linux, or run without --secure."
    )


def sandbox_available() -> bool:
    return have_seatbelt() or have_bwrap()
