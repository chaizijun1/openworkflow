meta = {
    "name": "code-review",
    "description": "Review a set of files in parallel, then synthesize prioritized findings.",
    "phases": ["review", "summary"],
}

# A reliable, name-callable template (best path for weaker models — no script authoring needed).
# Invoke:
#   openworkflow run-name code-review --args '["auth.py", "db.py"]'
#   Workflow(name="code-review", args={"files": ["auth.py"], "focus": "security & error handling"})
# `args` is tolerant: a list of files, a {"files": [...], "focus": "..."} dict, or a single string.


def _parse_args(spec):
    focus = "correctness, security, and clarity"
    if isinstance(spec, dict):
        files = spec.get("files") or spec.get("paths") or spec.get("targets") or []
        if isinstance(files, str):
            files = [files]
        focus = spec.get("focus") or focus
    elif isinstance(spec, (list, tuple)):
        files = list(spec)
    elif isinstance(spec, str) and spec.strip():
        files = [spec.strip()]
    else:
        files = []
    files = [str(f) for f in files if str(f).strip()]
    return (files or ["."]), focus


async def main():
    files, focus = _parse_args(args)
    log(f"reviewing {len(files)} target(s) for {focus}")

    # Phase 1: one reviewer per file, in parallel (BARRIER — synthesis needs them all).
    phase("review")
    reviews = await parallel(
        [
            (lambda f=f: agent(
                f"Review `{f}` for {focus}. List concrete issues as `file:line — problem — fix`, "
                f"most severe first. If you cannot read it, say so briefly.",
                {"label": f"review:{f}", "phase": "review"},
            ))
            for f in files
        ]
    )
    pairs = [(f, r) for f, r in zip(files, reviews) if r]  # drop failed reviewers (None)

    # Phase 2: synthesize into one prioritized report. Only this returns to the caller.
    phase("summary")
    if not pairs:
        return "code-review: no reviewable targets produced findings."
    body = "\n\n".join(f"## {f}\n{r}" for f, r in pairs)
    return await agent(
        "Merge these per-file reviews into a single prioritized list of findings "
        "(severity first, deduplicated). End with the top 3 actions.\n\n" + body,
        {"label": "summary"},
    )
