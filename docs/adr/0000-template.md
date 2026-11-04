---
id: ADR-NNNN
title: "Short title in the imperative or as a noun phrase"
status: Proposed
date: YYYY-MM-DD
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [area, area]
---

# ADR-NNNN: Short title in the imperative or as a noun phrase

| Status | Date | Deciders |
|---|---|---|
| Proposed | YYYY-MM-DD | SMART maintainers |

## Context

What situation forced a decision. The constraints that matter (where code runs, who edits it, what breaks if it is done the obvious way), in one or two paragraphs. Write each paragraph as one line.

## Decision

The decision in one or two sentences, stated so that a reader can tell whether a given change respects it. A Proposed record states the recommendation and ends with the question for the maintainers.

## Alternatives considered

- **Alternative A.** What it is, and why not.
- **Alternative B.** What it is, and why not.

## Consequences

What the decision costs and what it buys, including the sharp edges a contributor will meet. Say plainly what is planned but not shipped.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- One question per bullet.

## Enforced by

- `tests/test_something.py::test_function_name`: what it checks.
- `src/package/module.py`: the code that makes the decision hold without a test.

Name exact paths and test functions, and check that each one exists before opening the pull request. A Proposed record says "Nothing yet."

The front matter is read by tooling; the table under the title is the same information for a reader. Keep them equal. Status is one of `Proposed`, `Accepted`, `Superseded by ADR-NNNN`.
