---
name: implement
description: "Implement a piece of work based on a spec or set of tickets. Use when the user asks to build a spec or ticket, or `delegate` hands one over."
---

Implement the work described by the user in the spec or tickets.

If the user passes a ticket reference, fetch it from the issue tracker and state its title before starting. If the reference is ambiguous, ask.

Load the `tdd` skill where possible, at pre-agreed seams.

Run typechecking regularly, single test files regularly, and the full test suite once at the end.

Once done, load the `code-review` skill to review the work. Give it `git merge-base HEAD origin/<default-branch>` (the local default branch if there's no remote) as the fixed point, and the ticket as the spec.

Load the `commit` skill and commit your work to the current branch. On your own issue branch (`<id>-<slug>`), push it and open a draft PR/MR (command in the tracker doc, body from the `pr` skill). With no remote, stop after the commit. On any other branch, don't push.

Local tracker: update the ticket's status as `docs/agents/issue-tracker.md` says. A subagent working for `delegate` leaves that to the orchestrator.
