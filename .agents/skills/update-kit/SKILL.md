---
name: update-kit
description: Update this project's agent-kit skills and rules, and settle any conflicts with the user. Use when the user asks to update the kit, the skills, or agent-kit, or to check for kit updates.
---

# Update the kit

Run from the project root. Use `python` below, or `py` on Windows if `python` is missing.

## 1. Find the kit

Read `kit` from `.agents/agent-kit.local.json`. If the file is missing or that folder has no `init.py` (a fresh clone or another machine), ask the user where the kit is.

Done when `<kit>/init.py` exists.

## 2. Offer the latest kit

1. Run `git -C <kit> fetch`, then `git -C <kit> status -sb` and `git -C <kit> log --oneline 'HEAD..@{u}'`.
2. If the kit is behind, show those commits and ask whether to pull. Pull only with `git -C <kit> pull --ff-only`.
3. If the pull fails, or the kit has local changes or no upstream, tell the user and ask whether to continue with the kit as it is.

Done when the kit is current or the user chose to continue without pulling.

## 3. Check the project

Run `git status --short`. If there are uncommitted changes, tell the user they'd end up mixed with the update, and ask whether to continue.

## 4. Run the update

```bash
python <kit>/init.py . --update --no-input
```

Each undecided file in the output is `conflict` (the user edited it and the kit changed it) or `dropped` (the user edited it and the kit no longer ships it).

## 5. Settle every undecided file with the user

Ask about one file at a time, and let the user make every decision, recommending one option first. For each file, say in plain words what the kit changed and what the user would lose.

| Case | Options |
|---|---|
| `conflict` | Keep theirs, take the kit's, combine both, or decide later |
| `dropped` | Keep it as their own, delete it, or decide later |

Combine both: edit the user's file to include both changes and show them the result. Once they approve, it counts as keep.

Apply all the answers in one run, listing paths as the output shows them. `--take` replaces the file with the kit's, or deletes a dropped file. `--keep` leaves the file and stops asking until the kit changes it again.

```bash
python <kit>/init.py . --update --no-input --take <file> <file> --keep <file>
```

Done when every undecided file has an answer. "Later" leaves it undecided, and it comes back on the next update.

## 6. Check and report

Run `python <kit>/init.py --check .`. If it reports a problem, show it to the user.

Report the version change, what was added, updated, and removed, what the user decided, and anything left undecided. Then offer to commit with the `commit` skill, staging only the update: `AGENTS.md`, `.agents`, `docs/agents`, and `.gitignore`. Subject: `[chore]: update agent-kit to <version>`.
