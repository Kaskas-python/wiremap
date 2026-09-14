---
name: wiremap
description: Answer "where is this called", "what depends on this", "blast radius", "what does this file expose" with the wiremap CLI (framework-aware callers/deps/skeleton, never stale); and produce the context pack for any implementer or reviewer brief. Load before editing a symbol, before reading a whole file, when writing a brief, and when reviewing a diff.
---
# wiremap
1. Before editing a symbol: `wiremap callers <symbol>`. EXTRACTED = fact; INFERRED = lead to confirm; a non-zero `unresolved:` line means grep before assuming there are no callers.
2. Before reading a whole file: `wiremap skeleton <path>`; open the file only for the functions you will touch.
3. When writing a brief: `wiremap --repo <root> pack --files <paths> --task "<one line>"` and paste the output verbatim as the context pack.
4. When reviewing: `wiremap callers` on every changed public symbol; check each caller against the diff.
5. The tool never outranks the code: on any contradiction, STOP and report BLOCKED.
