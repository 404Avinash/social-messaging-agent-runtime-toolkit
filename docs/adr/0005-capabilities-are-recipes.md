---
id: ADR-0005
title: "Capabilities are recipes copied into the project, not plugins discovered at runtime"
status: Accepted
date: 2026-10-15
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [extensibility, recipes]
---

# ADR-0005: Capabilities are recipes copied into the project, not plugins discovered at runtime

| Status | Date | Deciders |
|---|---|---|
| Accepted | 2026-10-15 | SMART maintainers |

## Context

A WhatsApp agent wants capabilities beyond text: reply with buttons and lists, answer a voice note with a voice note, generate an image. Each is a small amount of agent-side code (a builder, a call to Polly or an image model, an upload through `notify.upload_media`) plus a prompt fragment telling the model when to use it. The question is how that code gets into a project and how the agent framework finds it.

A plugin system answers the second question for the framework: a manifest or an entry point lists what is installed, a loader discovers it at startup, and an include/exclude filter decides what the model sees. An earlier shape of the toolkit had a manifest and a loader, and the tests that forbid one today are what remains of removing them. The agent framework is the adopter's choice (Strands, LangGraph, custom), so a discovery mechanism either assumes one framework or grows an abstraction over all of them; a manifest is a second source of truth next to the code; and a tool the loader found but nobody wired is the hardest kind to debug, because nothing in the project names it.

## Decision

A capability is a **recipe**: a directory under `recipes/<name>/` with a `tool.py` (plain Python that imports only `socialagent_runtime` and the standard library), an optional `skill.md` prompt fragment, and a `README.md` with wiring instructions. `socialagent recipe add <name>` copies `tool.py` and `skill.md` byte for byte into the project's `src/tools/` and `src/skills/`, never overwrites an existing file, and writes no manifest. The adopter wires the copied function into their framework by hand (a Strands `@tool` wrapper in the project's `src/tools/`, for example) and the project's skill loader reads whatever markdown files `src/skills/` holds. There is no auto-discovery, no registry and no runtime filtering: a project's capabilities are the files it contains.

## Alternatives considered

- **A capability manifest with a loader and include/exclude filtering.** Framework-specific or abstracted over every framework, a second source of truth, and a tool that is loaded but unwired is invisible. Rejected, and the tests below keep it from coming back.
- **Python entry points on an installed package.** Needs the recipes to be an installed package, which they are not (see [ADR-0003](0003-agent-runtime-is-vendored.md)), and still leaves the framework wrapper to write by hand, so discovery saves nothing. Rejected.
- **Recipes as importable modules the project depends on rather than copies.** A recipe that is a dependency cannot be edited by the adopter, and the first thing adopters do with a recipe is change its wording or its defaults. Rejected; the copy is the adopter's to edit.

## Consequences

- Adding a capability is explicit: one command, then one line in the project pointing at the copied function. A feature where the system finds the thing automatically is questioned in review.
- A blueprint that ships with recipes pre-wired carries copies of the same `tool.py` files, and a test keeps those copies byte-identical to the recipe source so a fix lands in both or fails loudly.
- A fix to a recipe reaches an existing project only when the adopter copies it again. `recipe add` skips an existing file, so the update is a deliberate delete-and-add.
- Ruff does not lint `recipes/` or `blueprints/`: they are templates copied verbatim into projects, and a lint rule that rewrote them would change what adopters receive.
- The Strands `@tool` wrappers that make a recipe callable live in the project's `src/tools/`, so porting to another framework rewrites the wrappers and `agent.py` and nothing else.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- Should `recipe add` record what it copied (a manifest) so `runtime status` can report recipe drift the way it reports runtime drift? Today it writes nothing on purpose.

## Enforced by

- `src/socialagent/cli/recipe_cmd.py::cmd_recipe_add`: `dest_path.write_bytes(src_path.read_bytes())`, skip if the destination exists, no manifest.
- `tests/test_cli_recipe_outside_project.py::test_add_copied_content_matches_source`: the copied `tool.py` is byte for byte the recipe source.
- `tests/test_cli_recipe.py::test_recipe_add_copies_tool_to_destination`, `::test_recipe_add_copies_skill_when_present`, `::test_recipe_add_skips_existing_file_without_overwriting`, `::test_recipe_add_writes_no_manifest_anywhere`: what the command writes and what it refuses to write.
- `tests/test_cli_init.py::test_init_writes_no_socialagent_manifest_anywhere` and `::test_init_copies_no_manifest_yaml_anywhere`: scaffolding writes no manifest either.
- `tests/test_blueprint_recipe_copies.py::test_prewired_copy_matches_recipe`: every pre-wired copy in a blueprint equals `recipes/<name>/tool.py` byte for byte.
- `tests/test_architecture.py::test_recipes_do_not_import_control_layer`: a recipe's `tool.py` imports only `socialagent_runtime` and the standard library, so the copy runs in any project.
