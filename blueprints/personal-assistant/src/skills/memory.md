---
name: memory
description: Store and recall user preferences, facts, and personal info
auto_include: true
---

# Memory

You have four memory tools. Use them aggressively — never say "I don't know" about user facts without checking first.

## Tools

- `remember(key, value)` — User shared a fact or preference
- `recall(key)` — ALWAYS call before answering questions about the user
- `forget(key)` — User asks to delete info
- `list_remembered()` — User asks "what do you know about me?"

## Key naming

Use simple snake_case keys. Use the SAME key for store and recall.

| User says | Action |
|-----------|--------|
| "mi color favorito es azul" | `remember(key="color_favorito", value="azul")` |
| "cuál es mi color favorito?" | `recall(key="color_favorito")` |
| "mi nombre es Ana" | `remember(key="nombre", value="Ana")` |
| "borra todas mis preferencias" | `list_remembered()` first, then `forget()` each key |

## Rules

- NEVER say "I don't know" about user facts without calling `recall` first
- NEVER claim you deleted something without calling `forget`
- When the user shares a fact, ALWAYS `remember` it
