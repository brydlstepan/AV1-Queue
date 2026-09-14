---
name: changelog
description: Generate a changelog entry or release notes from commit history with bracketed type prefixes (`[feat]:`, `[fix]:`). Use when the user asks for a changelog, release notes, "what changed since vX", or is preparing a release.
---

# Changelog

The changelog is generated from commits on request. Nobody edits it by hand between releases.

## Steps

1. Find the range. Default: last tag to `HEAD` (`git describe --tags --abbrev=0`). If there are no tags, use the whole history and say so. If the user gives a range, use it.
2. If `git-cliff` is installed (`git cliff --version`) and the repo has a `cliff.toml` whose `commit_parsers` match `[type]:`, run `git cliff <range> --unreleased` (or `--tag <next>` when the user names the version) and use its output. Its default parser expects `type:` and drops these commits.
3. Otherwise read `git log <range> --no-merges --format='%h%x09%s%x09%b'` and group by type yourself:

   | Commit type | Section |
   |-------------|---------|
   | `[feat]` | Added |
   | `[fix]` | Fixed |
   | `[perf]`, `[refactor]` with visible effect | Changed |
   | anything with `]!:` or `BREAKING CHANGE:` | **Breaking** (first section) |
   | `[docs]`, `[test]`, `[build]`, `[ci]`, `[chore]` | leave out, unless the user wants everything |

   Older commits may use Conventional Commits (`feat:`). Group those the same way.

4. Rewrite each line for a reader who doesn't know the code: what changed for them, not the commit subject word for word.
5. Propose the next version from the commits (SemVer): breaking → major, any `feat` → minor, else patch. Before 1.0.0, breaking → minor.

## Output

Show the entry in this format ([Keep a Changelog](https://keepachangelog.com)):

```markdown
## [1.4.0] - 2026-10-27

### Breaking
- ...

### Added
- ...

### Fixed
- ...
```

Then ask where it goes: prepend to `CHANGELOG.md` (create it if missing), use as the GitHub/GitLab release body, or chat only. Don't create the tag or the release unless asked.
