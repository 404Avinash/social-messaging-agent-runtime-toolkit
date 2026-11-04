---
id: ADR-0008
title: "Distribution name on PyPI"
status: Proposed
date: 2026-10-15
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [packaging, naming]
---

# ADR-0008: Distribution name on PyPI

| Status | Date | Deciders |
|---|---|---|
| Proposed | 2026-10-15 | SMART maintainers |

## Context

The toolkit installs from a clone today. `pyproject.toml` names the project `socialagent`, the import package is `socialagent`, and the console script is `socialagent`. The roadmap plans a release on PyPI so that `init`, `dev` and the rest work without the clone, and plans to make the agent-side runtime a dependency instead of a vendored copy ([ADR-0003](0003-agent-runtime-is-vendored.md)). Both need a distribution name, and a distribution name is hard to walk back: once published, every install instruction and every `requirements.txt` points at it.

Two namespaces are involved, with different collision rules:

- **The distribution name** is what `pip install` takes. PyPI lowercases the name, strips `-`, `_` and `.`, maps `o` to `0` and `l`/`i` to `1`, and **rejects a new project whose result matches an existing one**. The project `social-agent` exists on the index, so `socialagent` cannot be registered. This is a hard block, not a risk.
- **The import name** is the top-level module a distribution installs. There is no registry, so nothing blocks a choice, but two distributions that install the same top-level module cannot coexist in one environment. `socialagent` is safe here: no distribution on the index installs a module by that name (the `social-agent` wheel installs `social_cli`, `src` and `tests`). A generic word such as `smart` is not safe: the distribution name `smart` is taken (a project with no files, which does not free the name), and `import smart` would collide with whatever any package installs under that module.

Names checked on the index on 2026-10-15:

| Name | State |
|---|---|
| `socialagent`, `social-agent`, `social_agent` | blocked (`social-agent` exists; the others normalize to it or are rejected as too similar) |
| `smart`, `smart-agent` | taken by unrelated projects |
| `smartagent` | blocked: normalizes to the same string as `smart-agent` |
| `smart-socialagent`, `smart-socialagent-runtime`, `smart-toolkit`, `smart-runtime` | free: no project normalized to the same string on 2026-10-15; only the first upload confirms it |

The distribution name and the import name do not have to match. `pip install <distribution>` followed by `import <module>` is a pattern Python users know, and the console script is independent of both. What must not change is the CLI, `socialagent`, because every document and every scaffolded project already says it.

## Decision

Proposed. The recommendation: publish under the distribution names **`smart-socialagent`** (the toolkit) and **`smart-socialagent-runtime`** (the agent-side package), keep the import package `socialagent` and the console script `socialagent` unchanged. The question for the maintainers is whether `smart-` is the right prefix, not whether a prefix is needed.

If accepted, `[project] name` in `pyproject.toml` changes to the prefixed name (the runtime package has no `pyproject.toml` today and gets one when it is published), the import package and the console script do not change, the README's install line reads `pip install smart-socialagent` with a note that the import and CLI are `socialagent`, and a test pins the places the distribution name appears.

## Alternatives considered

- **Register `socialagent` as-is.** Why not: PyPI rejects it; `social-agent` exists and the names are equal after removing separators. Not available.
- **Rename everything to a new word: distribution, import and CLI.** Removes any resemblance to the taken name. Why not: every document, blueprint, scaffolded project and every `import socialagent` an adopter has written changes, for a problem only the distribution name has.
- **Use `smart` as the import name.** Reads as the project's short name. Why not: the name is taken on the index, and `smart` is a module any package may install; `import smart` would be ambiguous in any environment that has another.
- **A different prefix than `smart-`.** Possible; the table above lists what is free. Why not yet: `smart-` is the project's short name and reads as such on the index. This is the open question below.
- **Do not publish; keep installing from a clone.** Why not: the roadmap's dependency story for the runtime and the CLI-without-a-clone story both need an index, and the first upload is the first release: it ends the source-only way the toolkit ships today, so it needs its own release and security review rather than happening by accident.

## Consequences

- Until this is decided, nothing is registered on PyPI and the toolkit installs from a clone. [ADR-0003](0003-agent-runtime-is-vendored.md) stays in force.
- If accepted: the distribution name appears in `pyproject.toml`, the README install line and, once it exists, the release workflow, and a test pins them. The import name and CLI appear everywhere else and do not change.
- The runtime package's name carries the same prefix, so the two read as a pair on the index.
- The row "Not published to any registry" in `docs/architecture-decisions.md` is updated when the decision lands.
- PyPI cannot reserve a name: a project exists only once a distribution file is uploaded, and an empty placeholder is name squatting under PEP 541. The first upload is therefore the first release, with its own review.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- Prefix: `smart-` is proposed; is there a better one that reads as the project's name and is free on the index?
- Timing: decide before the first release so the README never shows an install line that later changes, or defer until the PyPI story is scheduled?

## Enforced by

Nothing yet. A Proposed record enforces nothing; when the decision is made, the record is updated to Accepted, the distribution name is set in `pyproject.toml`, and a test pins the three places it appears.
