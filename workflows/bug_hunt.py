meta = {
    "name": "bug-hunt",
    "description": "Pipeline: for each suspect file, find candidate bugs then verify each — no barrier between stages.",
    "phases": ["find", "verify"],
}

# Demonstrates pipeline(): item A can be in 'verify' while item B is still in 'find'.
# Invoke:  openworkflow run-name bug-hunt --args '["auth.py", "db.py", "cache.py"]'


async def main():
    files = args or ["example.py"]

    async def find_stage(prev, file, index):
        phase("find")
        return await agent(
            f"List up to 3 likely bugs in `{file}` (one per line). File #{index}.",
            {"label": f"find:{file}", "phase": "find"},
        )

    async def verify_stage(candidates, file, index):
        phase("verify")
        return await agent(
            f"For `{file}`, confirm which of these are real bugs and how to fix:\n{candidates}",
            {"label": f"verify:{file}", "phase": "verify"},
        )

    # Each file flows find -> verify independently; wall-clock = slowest single file chain.
    results = await pipeline(files, find_stage, verify_stage)
    return {file: r for file, r in zip(files, results)}
