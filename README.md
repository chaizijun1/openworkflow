# openworkflow

*English | [简体中文](README.zh-CN.md)*

**An open-source alternative to Claude Code's `Workflow` tool** — the multi-agent orchestration
engine, brought to Python and usable anywhere (and pluggable back into Claude Code as an MCP tool,
where the native `Workflow` is hidden/unavailable in most versions).

It reproduces the execution model: the orchestrator writes a script; tool calls made inside the
script pause, execute, and return their result to the *running script* — **not** to the model's
context window — so only the script's final return value flows back. That is what makes cheap
fan-out over dozens of agents possible without flooding the context.

> **About.** openworkflow is an independent open-source take on the `Workflow` multi-agent
> orchestration concept from Claude Code — built from its publicly observable interface (primitive
> names, input shape, documented behavior). The implementation — runtime, sandbox, RPC, backends,
> tools — is entirely original; it bundles no third-party source. "Claude" / "Claude Code" are
> trademarks of their respective owner. Use at your own risk; please read
> **[Limitations & Risks](#limitations--risks)** before relying on it.

---

## 1. What Claude's Workflow is

Based on the publicly observable interface of Claude Code 2.1.150 (the `Workflow` tool ships but
does **not** appear in `/help`). The shape it exposes:

**Invocation** (by the main model):
```js
Workflow({name: "deep-research", args: "<question>"})   // run a saved, named workflow
Workflow({scriptPath: "<path>"})                          // re-run a script written earlier
```
Each invocation **persists the script to a file** under the session dir and returns the path,
so you iterate by editing that file and re-invoking with `{scriptPath}`.

**Script contract:**
- First statement must be `export const meta = { name, description, phases }` — a *pure
  literal*.
- `Date.now()` / `new Date()` are **unavailable** in workflow scripts: *"breaks resume."*
  Scripts must be deterministic so a crashed run can replay cached results (the binary contains
  a REPL-replay engine with drift detection: *"likely nondeterminism (Date.now, Math.random)
  took a different branch"*).

**Six in-scope globals** (verbatim semantics from the binary):

| primitive | semantics |
|---|---|
| `agent(prompt, opts?)` | Spawn a subagent. No `schema` → returns final text. With a JSON `schema` → subagent is forced to call a `StructuredOutput` tool and you get the validated object. Returns `null` if the user skips it. `opts`: `label`, `phase`, `schema`, `model`, `isolation:'worktree'`, `agentType`. |
| `parallel(thunks)` | **Barrier**: awaits all. A thunk that throws resolves to `null` (the call never rejects) → `.filter(Boolean)`. |
| `pipeline(items, ...stages)` | **No barrier** between stages: item A can be in stage 3 while B is in stage 1. Default for multi-stage work. Each stage gets `(prevResult, originalItem, index)`. A throwing stage drops that item to `null`. |
| `phase(title)` | Start a progress group; later `agent()`s are grouped under it. |
| `log(message)` | Narrator line above the progress tree. |
| `workflow(nameOrRef, args?)` | Run another workflow inline; shares this run's concurrency cap, agent counter, abort signal, and token budget. **One level of nesting only.** |
| `args` / `budget` | Input value / shared token ceiling. `budget = {total, spent(), remaining()}`; `total` is a **hard ceiling** — once `spent() ≥ total`, further `agent()` calls throw. Pool is shared across the run and all nested workflows. |

**Registry** (3 sources, precedence user > project > built-in):
`~/.claude/workflows/*.js`, `.claude/workflows/*.js`, and plugin-provided workflows. The binary
also defines a built-in `workflow-subagent` agent type and a `/workflows` view.

---

## 2. How this port maps the design to Python

| original (JS / vm sandbox) | this port (Python) |
|---|---|
| `export const meta = {...}` first statement | `meta = {...}` literal first statement (AST-validated) |
| script body uses globals, async | `async def main():` entrypoint; globals injected into module ns |
| `Promise` / `await` / `Promise.all` | `asyncio` coroutines / `asyncio.gather` |
| `vm.Script` sandbox (sealed globals) | two modes: in-process `compile()`+`exec()` (contract+determinism), or **`--secure`**: script runs under `sandbox-exec`/`bwrap` with primitives over an RPC pipe — kernel-enforced no-net/no-fs-egress |
| `Date.now`/`Math.random` banned | `time.*`, `datetime.now/utcnow`, `random/secrets/uuid` rejected at validation |
| journal + REPL replay (drift) | append-only JSONL journal keyed by `sha256(phase|label|prompt)`, replays completed `agent()` results, reports drift |
| workflow-subagent with `tools:["*"]` | `ToolAgentBackend`: a real multi-turn tool-use loop over Read/Write/Edit/Bash/Grep/Glob/WebFetch/WebSearch/NotebookEdit + any MCP tools — the subagent does actual work. Also `AnthropicBackend` (single text/structured call) and `MockBackend` (zero-cost). |
| MCP servers / `mcp__*` tools | `MCPManager` connects servers from `~/.openworkflow/mcp.json` (stdio + http/sse) and exposes their tools namespaced `mcp__<server>__<tool>` — one integration for the whole MCP ecosystem |
| `Workflow({name/script/scriptPath})` tool (in Claude Code) | `mcp_server.py` exposes openworkflow as an MCP `Workflow` tool, so any MCP-capable Claude Code can call it like the native (hidden) one — subagents via API key or via client **sampling + tools** |
| `opts.isolation:'worktree'` | honored — creates a detached git worktree per agent, runs it there, auto-removes if unchanged, keeps + reports the path if modified |
| `opts.agentType` (custom subagent) | resolved to a system-prompt override from `~/.openworkflow/agents/<type>.md` |
| plugin workflows (`<plugin>:<name>`) | scanned from `~/.openworkflow/plugins/*/workflows/*.py`, namespaced; precedence built-in < plugin < project < user |
| `/workflows` progress tree | `progress.py` phase/log narrator (rich optional) |
| `~/.claude/workflows` etc. | `~/.openworkflow/workflows`, `./.openworkflow/workflows`, built-in `workflows/` |

---

## 3. Install & run

```bash
cd openworkflow
pip install -e .                 # core (zero deps)
pip install -e '.[anthropic]'    # to use the real Anthropic backend

# list saved workflows
openworkflow list

# run a saved workflow by name (mock backend = free, deterministic)
openworkflow run-name deep-research --args '"Tradeoffs of vector DBs?"' --backend mock

# run a script file, with a hard token budget and structured JSON output
openworkflow run examples/voting.py --args '"Is 113 prime?"' --budget 300000 --json

# real LLM: set the key and the backend auto-switches
export ANTHROPIC_API_KEY=sk-...
openworkflow run-name deep-research --args '"..."'   # uses AnthropicBackend (text/structured)

# tool-using subagents that actually read/write files & run shell (faithful subagent)
openworkflow run-name bug-hunt --args '["auth.py"]' --backend tool

# resume a crashed run — completed agent() calls replay for free
openworkflow run-name bug-hunt --args '["a.py","b.py"]' --journal run.jsonl
openworkflow run-name bug-hunt --args '["a.py","b.py"]' --journal run.jsonl --resume
```

### Use it inside Claude Code (as the `Workflow` tool)

The native `Workflow` tool is hidden/unavailable in most Claude Code versions. openworkflow ships
an **MCP server** that gives Claude Code a `Workflow` tool with the same inputs
(`script` / `name` / `scriptPath`, `args`, `budget`) — works in any CC version with MCP support.

```bash
pip install -e '.[mcp,anthropic]'
claude mcp add openworkflow -- python3 -m openworkflow.mcp_server
# or in .mcp.json:
# {"mcpServers": {"openworkflow": {"command": "python3", "args": ["-m", "openworkflow.mcp_server"]}}}
```

Now Claude can call `Workflow(script="…", args=…)` exactly like the original: it writes an
orchestration script, the server runs it on your machine, and **only the final result returns to
the context window**.

**Where does the subagent LLM come from?** (`OPENWORKFLOW_BACKEND=auto` by default)
- **`ANTHROPIC_API_KEY` set** → `ToolAgentBackend`: subagents run native tool_use loops
  (Read/Write/Edit/Bash/Grep/Glob/Web/MCP). Most capable. Billed via the API key.
- **No key** → `SamplingToolBackend`: subagents borrow **Claude Code's own model** via MCP
  *sampling*, AND still get tools. MCP sampling is plain completion (no tools field), so we layer
  a ReAct text protocol: each reasoning turn runs on the client model, the model emits a
  `{"tool": …}` / `{"final": …}` JSON, and **tools execute locally on the server**. So method A
  subagents can do real work — reasoning on the client model, tools on your machine. (Verified
  end-to-end through a real MCP sampling round-trip.) Force modes with
  `OPENWORKFLOW_BACKEND=tool|anthropic|sampling|sampling-tools|mock`; `OPENWORKFLOW_SECURE=1`
  sandboxes the script.

Trade-off: the sampling path needs a client that supports MCP sampling, and the text protocol is
less rigid than native tool_use (a weak model may drift). With a key, the native tool loop is
more reliable.

### Autonomous design (self-designing workflows)

Like the original — where the main model *writes the orchestration script itself* — openworkflow
can author a workflow from a plain task. The brain (`author.py`) prompts an LLM to emit a script
obeying the contract, validates it with the same `compile_script` the runtime uses, and feeds
validation errors back for repair; then the body (runtime) runs it.

```bash
# just design and print the script
openworkflow design "Research the tradeoffs of GraphQL vs REST"

# design AND run, end-to-end (no human-written script)
openworkflow do "Compare three caching strategies and recommend one" --budget 200000
export ANTHROPIC_API_KEY=sk-...    # with a key, the LLM genuinely authors the design
openworkflow do "Audit each service for N+1 queries" --args '["users","orders","billing"]'
```

Offline (mock / no key) a deterministic *scaffold designer* picks structure from task cues
(parallel fan-out + synthesis, or a per-item pipeline) so the full design→validate→run loop is
runnable for free. Library API: `design_workflow(task, backend=...)` and
`do_task(task, backend=...) -> (WorkflowResult, DesignResult)`.

### Writing a workflow

```python
meta = {"name": "deep-research", "description": "...", "phases": ["angles", "research", "synthesis"]}

async def main():
    phase("angles")
    plan = await agent("Break this into angles:\n" + args,
                       {"schema": {"type": "object",
                                   "properties": {"angles": {"type": "array",
                                                             "items": {"type": "string"}}}}})
    phase("research")
    findings = await parallel([(lambda a=a: agent(f"Research: {a}", {"phase": "research"}))
                               for a in plan["angles"]])
    phase("synthesis")
    return await agent("Synthesize:\n" + "\n".join(f for f in findings if f))
```

The library API mirrors this too:
```python
import asyncio
from openworkflow import run_workflow, MockBackend
res = asyncio.run(run_workflow(open("examples/voting.py").read(),
                               args="Is 113 prime?", backend=MockBackend(), budget_total=200_000))
print(res.result, res.agent_count, res.spent_tokens)
```

### Tool-using subagents + MCP

With the tool backend, each `agent()` runs a real multi-turn loop and can read/write files, run
shell, search, fetch URLs, search the web, and edit notebooks:

```bash
pip install -e '.[anthropic]'
export ANTHROPIC_API_KEY=sk-...
pyworkflow run-name bug-hunt --args '["auth.py"]' --backend tool   # subagents do real work
```

Built-in tools: `Read Write Edit Bash Grep Glob WebFetch WebSearch NotebookEdit`. WebSearch needs
`TAVILY_API_KEY` or `BRAVE_API_KEY`; WebFetch and Bash (`curl`) work with no key.

**MCP — one integration, the whole ecosystem.** Drop a Claude-compatible `~/.openworkflow/mcp.json`:

```json
{"mcpServers": {
  "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]},
  "github":     {"command": "uvx", "args": ["mcp-server-github"], "env": {"GITHUB_TOKEN": "..."}},
  "remote":     {"url": "https://mcp.example.com/sse"}
}}
```

```python
from openworkflow import ToolAgentBackend, MCPManager, run_workflow
backend = ToolAgentBackend(mcp=MCPManager())   # connects servers, exposes mcp__<server>__<tool>
```

Subagents then see every MCP tool alongside the built-ins (namespaced `mcp__github__create_issue`,
etc.). Needs `pip install -e '.[mcp]'`; absent SDK/config is a graceful no-op.

### Secure mode — real kernel-enforced sandbox

By default the script runs in-process (fast, *not* a security boundary). With `--secure` (or
`run_workflow(..., secure=True)`) the **script runs inside an OS sandbox** and the six primitives
are served over an RPC pipe from the trusted host:

```
┌─ host (trusted) ───────────────┐   pipe   ┌─ sandbox-exec child (untrusted) ──┐
│ WorkflowRuntime                │◄──RPC───►│ async def main():                 │
│  real LLM/tool calls, API key, │  (only   │   await agent(...) → RPC request  │
│  budget, journal, worktrees    │  workflow│ kernel: NO network, NO writes     │
│                                │  methods)│ outside scratch, secret dirs read-│
└────────────────────────────────┘          │ denied. Only channel out = pipe.  │
                                             └────────────────────────────────────┘
```

```bash
openworkflow run untrusted.py --secure --backend mock
```

**Backends:** macOS uses `sandbox-exec` (Seatbelt — ships with the OS, kernel-enforced, the same
mechanism Claude Code/Cursor/OpenClaw use); Linux uses `bubblewrap` (`bwrap --unshare-net`).

**Threat model / what's enforced by the kernel:**
- **No network egress** — every non-localhost `connect()` gets `EPERM`, so even a script that
  reads a secret can't exfiltrate it.
- **No filesystem writes** outside a per-run scratch dir.
- **Secret dirs read-denied** (`~/.ssh`, `~/.aws`, `~/.gnupg`, …) as defense-in-depth.
- **No API keys in the child env** — the host holds the key and makes the LLM calls; the sandbox
  only orchestrates.
- The script **cannot opt out** — a Seatbelt profile inherits to children and can't be removed
  from inside.

This is the one gap that the in-process executor couldn't close: with `--secure`, running an
*untrusted* workflow script is actually safe. (Verified by tests:
`tests/test_secure.py::test_secure_blocks_network_and_filesystem`.)

**Cost:** ~tens of ms to spawn the sandboxed child per run; RPC multiplexes concurrent `agent()`
calls so `parallel`/`pipeline` keep their concurrency.

---

## 4. Faithful vs. deliberately different

**Faithful:** the six primitives and their exact semantics (barrier vs no-barrier, failures→None,
schema→validated object), `meta`-first contract, determinism ban, shared hard budget ceiling,
one-level `workflow()` nesting, three-source registry with user>project>built-in precedence,
journal-based resume with drift detection, structured output via a forced `StructuredOutput`
tool.

**Different on purpose:**
- Scripts are **Python**, not JS (`async def main()` entrypoint instead of a bare async body).
- Two execution modes: in-process (`exec`, fast, *contract+determinism* only — for your own
  scripts) and **`--secure`** (OS-sandboxed subprocess + RPC, kernel-enforced — for untrusted
  scripts). The original uses one V8 `vm` sandbox; we trade that for a process boundary because
  Python `exec` can't isolate in-process. The security guarantee is different in mechanism but
  comparable in effect (no net, no fs egress).
- Subagents can be **tool-using** (`ToolAgentBackend`: a real multi-turn loop over
  Read/Write/Edit/Bash/Grep/Glob/**WebFetch/WebSearch/NotebookEdit** plus any **MCP** tools),
  or plain single-call (`AnthropicBackend`), or free (`MockBackend`).
- Resume keys on `(phase|label|prompt)` hashes (robust to async scheduling) rather than a
  byte-exact REPL replay.

---

## Limitations & Risks

Read this before relying on the project. These are real and intentional to surface.

### Security
- **In-process mode (`exec`) is NOT a security boundary.** It only enforces the script contract
  + determinism. A malicious script can do anything your Python process can. **Never run
  untrusted scripts without `--secure`.**
- **`--secure` is OS-level, not VM-level.** It uses macOS `sandbox-exec` (Seatbelt) or Linux
  `bubblewrap`. It blocks network egress and out-of-scratch writes (kernel-enforced, verified by
  tests) but it is **not** a hypervisor/microVM. Caveats: `sandbox-exec` is officially
  *deprecated* by Apple (still works, widely used); Linux needs `bwrap` installed; **no Windows
  support**; reads are allowed by default (egress is what's blocked), so treat it as "can't
  exfiltrate / can't persist outside scratch", not "can't read anything".
- **Subagent tools run with your privileges on the host.** `Bash`, `Write`, etc. in
  `ToolAgentBackend` act as you. Scope with `ToolBox(confine=True)`, `allow_bash=False`, or run
  the orchestration `--secure`. The sandbox confines the *script*, not necessarily every action
  a tool-using subagent takes on the host.

### Claude subscription / Terms-of-Service risk (sampling backends)
- `OPENWORKFLOW_BACKEND=sampling` / `sampling-tools` route subagent calls to the **MCP client's
  model — i.e. a user's Claude Code subscription.** A fan-out workflow issues many completions;
  on a consumer plan this can **hit rate limits** and may **violate Terms of Service**
  (programmatically scaling consumer access). **This carries account risk and is the user's
  responsibility.**
- Mitigations are built in: sampling is **opt-in only** (never the silent default — no key falls
  back to `mock`), concurrency is clamped to 1, a conservative token budget is applied, and a
  warning prints on stderr. **For any real volume, use an API key (`ToolAgentBackend`), which is
  the supported, pay-per-token, ToS-clean path.**
- We are not lawyers; this is not legal advice. Check Anthropic's current Terms before using the
  sampling path.

### Correctness / reliability
- **Sampling-tools uses a ReAct text protocol**, not native tool_use. A weaker client model may
  emit malformed JSON or drift; it is best-effort, less reliable than the API tool loop.
- **Subagent quality = your backend's model.** `MockBackend` is for plumbing tests only (echoes,
  no reasoning). Real results need `anthropic`/`tool` backends with a capable model.
- **Resume is hash-based**, not byte-exact replay. Determinism is enforced by banning clocks/RNG
  in scripts, but heavy nondeterminism via tool results can still cause drift (reported, not
  silently wrong).
- **`exec()` of scripts** means a syntax/logic error surfaces as a Python exception, not a tidy
  validation message (beyond the `meta`/determinism checks).

### Scope gaps vs. the original
- Tool registry is the core set (file/shell/search/web/notebook) **plus any MCP server** — not
  Anthropic's full internal tool set.
- No interactive `/workflows` UI, no mid-run "skip this agent", no byte-exact replay, no
  auto-persist-to-session-dir. These are ergonomics/fidelity, not capability.

### Dependencies / platform
- Core has **zero dependencies**. Optional extras: `anthropic` (real LLM), `mcp` (MCP client +
  the Claude Code server bridge). `WebSearch` needs `TAVILY_API_KEY` or `BRAVE_API_KEY`.
- `--secure` works on **macOS and Linux only**. Everything else is cross-platform.

---

## Recommendations (when to use what)

| You want to… | Use |
|---|---|
| Orchestrate LLM + file/code/shell work, real results | API key + `--backend tool` (`ToolAgentBackend`) |
| Run an **untrusted / model-written** script safely | add `--secure` (macOS/Linux) |
| Plug into Claude Code as a `Workflow` tool, with budget | MCP server + `ANTHROPIC_API_KEY` |
| Use Claude Code's own model for subagents | `OPENWORKFLOW_BACKEND=sampling-tools` — **only if you accept the subscription/ToS risk above**, small fan-outs only |
| Develop/test orchestration logic for free | `MockBackend` (`--backend mock`) |
| Reach databases / SaaS / GitHub etc. | configure MCP servers in `~/.openworkflow/mcp.json` |

**Rules of thumb:** default to an **API key** for anything beyond a demo; reserve **sampling** for
tiny, interactive, opt-in use; turn on **`--secure`** whenever the script isn't one you wrote;
keep **`MockBackend`** for CI and plumbing tests.

---

## 5. Layout

```
openworkflow/
├── openworkflow/
│   ├── runtime.py     # the 6 primitives + execution (the heart)
│   ├── author.py      # autonomous design: LLM writes the script, validate→repair→run
│   ├── backends.py    # AgentBackend: Mock / Anthropic / ToolAgent (tool-use loop)
│   ├── tools.py       # ToolBox: Read/Write/Edit/Bash/Grep/Glob/WebFetch/WebSearch/NotebookEdit
│   ├── mcp_client.py  # MCPManager: connect MCP servers, expose mcp__server__tool
│   ├── mcp_server.py  # expose openworkflow to Claude Code as a `Workflow` tool (+ sampling)
│   ├── worktree.py    # git worktree isolation for parallel file-mutating agents
│   ├── secure.py      # host: drive a sandboxed script over RPC (--secure)
│   ├── seatbelt.py    # OS sandbox launchers: sandbox-exec (macOS) / bwrap (Linux)
│   ├── _sandbox_child.py  # the untrusted script's sandboxed entry point
│   ├── _rpc.py        # length-prefixed JSON framing over the pipe boundary
│   ├── budget.py      # shared hard token ceiling
│   ├── journal.py     # resume + drift detection
│   ├── sandbox.py     # meta-first contract + determinism validation
│   ├── registry.py    # user/project/built-in workflow discovery
│   ├── progress.py    # phase/log narrator
│   └── cli.py         # `openworkflow` command
├── workflows/         # built-in saved workflows (deep-research, bug-hunt)
├── examples/          # voting.py (parallel + budget scaling + sub-workflow)
└── tests/             # 60 tests, all on the zero-cost mock backend / fakes
```

---

## Tests

```bash
pip install pytest
python3 -m pytest tests/ -q
```

Tests run on the zero-cost `MockBackend` / fakes by default; `--secure` end-to-end cases skip
automatically when no OS sandbox is available.

---

## License

MIT — see [LICENSE](LICENSE). See **About** at the top for positioning.
