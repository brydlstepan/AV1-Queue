---
name: grilling
description: Grill the user relentlessly about a plan, decision, or idea. Use when the user wants to stress-test their thinking, or uses any 'grill' trigger phrases.
---

Interview the user relentlessly until you reach a shared understanding. Map this as a **design tree**: every decision branches into the decisions that hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled: the questions you can ask _now_ without guessing at answers you haven't heard yet. Ask the whole frontier in one round: number each question and give your recommended answer. Then wait for the user's answers before the next round.

## Asking a round

**If you have a structured question tool, use it.** Examples: `AskUserQuestion` in Claude Code, the ask-question / clarifying-questions tool in Cursor. The user then clicks answers instead of typing them.

- One question per decision. Give 2-4 concrete options; put your recommendation first and end its label with `(Recommended)`. Each option's description says the trade-off in one line.
- Keep labels short (1-5 words) and headers to a chip-sized tag (e.g. `Stack`, `Backups`). The tool adds a free-text "Other" option itself; don't add one.
- Use multi-select when choices can be combined (e.g. "which of these skills do you want?").
- When comparing concrete artefacts (config snippets, folder layouts, UI mockups), put them in the option's preview if the tool supports one.
- The tool takes only a few questions per call (Claude Code: 4). For a bigger frontier, ask the most consequential 4 now and the rest in the next call of the same round.
- A question with no sensible fixed options (e.g. "what's the service called?") still goes through the tool: offer your best guesses as options and let the user pick "Other".

**Otherwise, write the round as text**, like so:

```
❓ **Q1** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>

---

❓ **Q2** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>
```

Word each question so "yes" accepts your recommended answer.

After each round, show a short table of the decisions settled so far (`# | Decision | Answer`) so the user can see the tree filling in.

## Between rounds

Each round the user answers reshapes the tree: settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round. A question whose answer depends on another question still open in this round belongs to a _later_ round, not this one.

Finding _facts_ is your job, never the user's. When a frontier question needs a fact from the environment (filesystem, tools, etc.), dispatch a sub-agent to find it; don't ask the user for anything you could look up yourself. Don't block on it: a running exploration is an unsettled prerequisite, so only the questions downstream of it wait for the sub-agent to report; ask the rest of the frontier now. The _decisions_ are the user's: put each to them and wait.

The session is done when the frontier is empty: every branch of the design tree visited, nothing left silently assumed. End with the full decisions table and ask the user to confirm it (with the question tool, if you have one). Do not act on it until the user confirms you have reached a shared understanding.
