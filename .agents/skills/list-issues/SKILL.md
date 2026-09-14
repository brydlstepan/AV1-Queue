---
name: list-issues
description: List open issues and sort them into ready to delegate, needs its own session, blocked, or in progress, then wait for the user's pick. Use when the user asks what to work on next or what issues there are.
---

# List issues

You are the **orchestrator**: you sort and hand off, you don't implement. Keep your own context light.

## Steps

1. Read `docs/agents/issue-tracker.md` and list open issues with it. Put issues labelled `ready-for-agent` first.
2. Find work already under way: `git branch -a --list '*<id>-*'`, `git worktree list`, and open PRs/MRs (local tracker: `Status: doing`).
3. Put every open issue in exactly one bucket:

   | Bucket | Test |
   |---|---|
   | **Ready to delegate** | Has acceptance criteria (a checkable "done"), names or clearly implies the files it touches, and has no unresolved questions in the body or comments. |
   | **Needs a session** | Fails any test above, or is big enough to need a plan first. Say which test it fails. |
   | **Blocked** | Has an open blocker (see the tracker doc). |
   | **In progress** | A branch, worktree, or PR for it exists. |

4. Show one table: `# | Title | Bucket | Why`. Recommend up to three to start, and say which can run in parallel (no shared files).
5. Ask the user which to start, with your recommendation first. Wait for the answer.
6. For each pick:
   - **Ready to delegate** → load `delegate` (if this project has it).
   - **Needs a session** → give the user a starter line for a new session in its own worktree, e.g. `Work on #142 (login timeout). Start with grilling.` (add `then to-spec` if this project has it), plus the worktree command from `delegate` if it's installed.

Done when every pick has been handed to `delegate` or the user has its starter line.
