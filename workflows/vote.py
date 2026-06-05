meta = {
    "name": "vote",
    "description": "Answer the same question N times in parallel and report the majority verdict.",
    "phases": ["vote", "tally"],
}

# A reliable, name-callable template (best path for weaker models — no script authoring needed).
# Self-consistency / majority vote: useful to stabilize a flaky single answer.
# Invoke:
#   openworkflow run-name vote --args '"Is 113 prime?"'
#   Workflow(name="vote", args={"question": "Is 113 prime?", "n": 5})
# `args` is tolerant: a question string, or a {"question": ..., "n": <voters>} dict. Without an
# explicit n, the fleet scales to `budget` (documented static-scaling pattern), min 3.


def _parse_args(spec):
    if isinstance(spec, dict):
        question = spec.get("question") or spec.get("q") or spec.get("prompt") or "Pick a number 1-3."
        raw_n = spec.get("n") or spec.get("voters") or spec.get("fleet")
        try:
            n = int(raw_n) if raw_n is not None else None
        except (TypeError, ValueError):
            n = None
    elif isinstance(spec, str) and spec.strip():
        question, n = spec.strip(), None
    else:
        question, n = "Pick a number 1-3.", None
    return question, n


async def main():
    question, n = _parse_args(args)
    fleet = n if n else ((budget.total // 100_000) if budget.total else 5)
    fleet = max(3, fleet)
    log(f"voting on {question!r} with a fleet of {fleet}")

    phase("vote")
    votes = await parallel(
        [
            (lambda i=i: agent(
                f"{question}\nAnswer in one short line with your single best answer. (voter {i})",
                {"label": f"voter-{i}", "phase": "vote"},
            ))
            for i in range(fleet)
        ]
    )
    votes = [v for v in votes if v]

    phase("tally")
    verdict = await agent(
        "Here are independent answers to the same question. State the MAJORITY answer in one line, "
        "then note the vote split.\n" + "\n".join(f"- {v}" for v in votes),
        {"label": "tally"},
    )
    return {"verdict": verdict, "votes": votes, "fleet": fleet}
