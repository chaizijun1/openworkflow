meta = {
    "name": "deep-research",
    "description": "Fan out subagents to research a question from multiple angles, then synthesize.",
    "phases": ["angles", "research", "synthesis"],
}

# A saved workflow. Invoke with:
#   openworkflow run-name deep-research --args '"What are the tradeoffs of vector DBs?"'
# or inline from another workflow:  await workflow("deep-research", question)


async def main():
    question = args or "What should we research?"

    # Phase 1: ask one agent to break the question into angles (structured output).
    phase("angles")
    plan = await agent(
        f"Break this research question into 3-5 independent angles to investigate.\n\n{question}",
        {
            "label": "decompose",
            "schema": {
                "type": "object",
                "properties": {
                    "angles": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    )
    angles = (plan or {}).get("angles") or [question]
    log(f"{len(angles)} angle(s) to research")

    # Phase 2: research each angle in parallel (BARRIER — we need all before synthesis).
    phase("research")
    findings = await parallel(
        [
            (lambda a=a: agent(f"Research this angle of '{question}': {a}",
                               {"label": a[:40], "phase": "research"}))
            for a in angles
        ]
    )
    findings = [f for f in findings if f]  # drop any failed agents (None)

    # Phase 3: synthesize. Only this final string returns to the caller/model.
    phase("synthesis")
    report = await agent(
        "Synthesize these findings into a tight briefing with a recommendation.\n\n"
        + "\n\n".join(f"- {f}" for f in findings),
        {"label": "synthesize"},
    )
    return report
