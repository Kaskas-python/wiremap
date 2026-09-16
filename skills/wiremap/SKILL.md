---
name: wiremap
description: Framework-aware callers, deps, skeletons, context packs and review-time impact lists from the wiremap CLI, never stale. For Fable writing briefs, reviewers and researchers; implementers get the pack inside their brief.
---
# wiremap
`--repo <root>` goes before the subcommand; a worktree-isolated agent passes the main tree.
Every caller list is a candidate list: string dispatch (`send_task`, `getattr`) is invisible, so grep the bare name before concluding "no callers". EXTRACTED = fact; INFERRED = name-only lead.
1. Implementer brief (Fable): `wiremap --repo <root> pack --files <paths> --task "<one line>"`, pasted verbatim under your own assertion lines (imports present or absent, prohibitions, nullability, each with file:line). A file under ~60 lines is pasted instead of packed.
2. Review brief (Fable): `wiremap --repo <root> impact --base <target>`, pasted verbatim. Reviewer: every listed caller outside the diff is a finding or one line "unaffected because …".
3. Reviewer on demand: `wiremap callers <symbol id>` for a changed public symbol the impact list capped; `triage --base <target>` for the files to read.
4. Researcher: `skeleton <file>` before opening a file, `deps` and `callers` for the hops; cite file:line.
5. The tool never outranks the code: on any contradiction, STOP and report BLOCKED.
