---
name: feature
description: Plan a feature with grilling, create its issue, then start it, delegate it, or leave it for later.
disable-model-invocation: true
---

# Feature

Plan first, build after the user chooses how. If the user named a feature, start from that; otherwise ask what they want to build.

## 1. Grill the user

Load `grilling`. Look up in the code what you can, so the questions are only about decisions. Cover:

- The problem, and who has it
- What the feature does, including edge cases and failure behaviour
- What's out of scope
- How anyone can check it works
- Which parts of the code it touches

Done when the user has confirmed the decisions table.

## 2. Create the issue

Read `docs/agents/issue-tracker.md` for how this project's tracker works. If it's missing, load `tracker` first.

Choose one label and publish the issue:

| The feature... | Label |
|---|---|
| fits one fresh session, with no open questions | `ready-for-agent` |
| fits one fresh session, with open questions | none |
| is too big for one session | `spec` |

Local tracker: write a ticket at `docs/tickets/<feature-slug>/01-<slug>.md` with the header from the tracker doc, using `Status: todo`, `Blocked by: none`, and `Spec: none`. Leave `Labels:` empty if the table gives no label.

Use the settled decisions only. Paths are expected here, because `delegate` needs them and the issue is meant to be built soon.

```markdown
## Problem
<who has it, from their perspective>

## What to build
<the behaviour, including edge cases and failures>

## Acceptance criteria
- [ ] <something anyone can check>

## Where it touches
<files and modules, with what changes in each>

## Decisions
- <decision, and why>

## Testing
<what to test, at which seam, and similar tests to follow>

## Out of scope
- <what this deliberately leaves out>

## Open questions
- <only if the user left some open; otherwise leave this section out>
```

Done when the issue exists and you've given the user its number and link.

## 3. Ask what's next

Offer the options below that fit, recommending one first. Wait for the answer.

| Option | Offer it when | What you do |
|---|---|---|
| **Delegate to an agent** | `ready-for-agent` | Load `delegate` with this issue. |
| **Start here** | One session is enough | Follow **Starting here** below. |
| **Start in a new session** | One session is enough | Give the user a starter line, e.g. `Work on #142 (login timeout). Load implement.`, plus the worktree commands from `delegate`. A long grilling leaves less room in this session, so recommend this over starting here after one. |
| **Break it into tickets** | `spec` | Tell the user to run `to-tickets` with the issue number. |
| **Leave it for later** | Always | Stop. `list-issues` will find it. |

For open questions, first settle them with the user and update the issue. Then add `ready-for-agent` if the feature fits one session.

## Starting here

1. Run `git status --short`. If there are uncommitted changes, ask the user how to handle them.
2. Run `git fetch origin`, then `git switch -c <id>-<slug> --no-track origin/<default-branch>`. With no remote, branch from the local default branch. `<id>` is the issue number, or `<feature-slug>-01` for a local ticket.
3. Local tracker: set the ticket to `Status: doing`.
4. Load `implement` with this issue.
