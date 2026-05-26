meta = {
    "name": "voting-demo",
    "description": "Ask the same question N times in parallel and take a majority vote; also shows budget-driven fleet sizing and an inline sub-workflow.",
    "phases": ["vote", "tally"],
}

# Run directly:
#   openworkflow run examples/voting.py --args '"Is 113 prime?"' --budget 200000
# With a budget set, the fleet size scales to the budget (static scaling pattern from the docs).


async def main():
    question = args or "Pick a number 1-3."

    # Static scaling on budget, exactly like the documented pattern:
    #   const FLEET = budget.total ? Math.floor(budget.total / 100_000) : 5
    fleet = (budget.total // 100_000) if budget.total else 5
    fleet = max(3, fleet)
    log(f"voting with a fleet of {fleet}")

    phase("vote")
    votes = await parallel(
        [
            (lambda i=i: agent(f"{question} Answer in one short line. (voter {i})",
                               {"label": f"voter-{i}", "phase": "vote"}))
            for i in range(fleet)
        ]
    )
    votes = [v for v in votes if v]

    phase("tally")
    # delegate the tally to a saved sub-workflow if available, else inline.
    verdict = await agent(
        "Given these answers, state the majority verdict in one line:\n"
        + "\n".join(f"- {v}" for v in votes),
        {"label": "tally"},
    )
    return {"verdict": verdict, "votes": votes, "fleet": fleet}
