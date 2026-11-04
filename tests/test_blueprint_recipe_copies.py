# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A blueprint's pre-wired recipe copy stays byte-identical to its recipe.

`personal-assistant` ships recipes already copied into `src/tools/`, which is
what `socialagent recipe add` would have produced. If the copy drifts (a fix
lands in one place, or a formatter rewrites only one), a project scaffolded
from the blueprint and one that runs `recipe add` get different code.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# (recipe, pre-wired copy).
PREWIRED = [
    ("voice-reply", "blueprints/personal-assistant/src/tools/voice_reply.py"),
    ("generate-image", "blueprints/personal-assistant/src/tools/generate_image.py"),
    ("rich-messages", "blueprints/personal-assistant/src/tools/rich_messages.py"),
    ("rich-messages", "blueprints/appointment-scheduler/src/tools/rich_messages.py"),
]
IDS = [copy.split("/")[1] + ":" + recipe for recipe, copy in PREWIRED]


@pytest.mark.parametrize(("recipe", "copy"), PREWIRED, ids=IDS)
def test_prewired_copy_matches_recipe(recipe: str, copy: str) -> None:
    source = (REPO / "recipes" / recipe / "tool.py").read_bytes()
    assert (REPO / copy).read_bytes() == source, (
        f"{copy} differs from recipes/{recipe}/tool.py; copy the recipe over it"
    )


# The blueprints' `attach_interactive` wraps build_rich_message. Its own
# latitude/longitude defaults must be None, as the recipe's are: a default of 0
# reaches build_rich_message as a real coordinate, so "no location given" would
# silently send a pin at 0,0 instead of being rejected.
WRAPPERS = [
    "blueprints/personal-assistant/src/tools/__init__.py",
    "blueprints/appointment-scheduler/src/tools/__init__.py",
]


@pytest.mark.parametrize("path", WRAPPERS, ids=[p.split("/")[1] for p in WRAPPERS])
def test_attach_interactive_coordinates_default_to_none(path: str) -> None:
    tree = ast.parse((REPO / path).read_text())
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name == "attach_interactive"
    )
    args = fn.args.args + fn.args.kwonlyargs
    defaults = dict(
        zip(
            [a.arg for a in fn.args.args][-len(fn.args.defaults) :],
            fn.args.defaults,
            strict=True,
        )
    )
    defaults |= {a.arg: d for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d}
    for name in ("latitude", "longitude"):
        assert name in {a.arg for a in args}, f"{path}: no {name} parameter"
        default = defaults[name]
        assert isinstance(default, ast.Constant) and default.value is None, (
            f"{path}: attach_interactive({name}=...) must default to None"
        )
