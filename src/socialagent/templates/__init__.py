# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Template management: registry, sync, and rendering.
"""

from socialagent.templates.registry import TemplateRegistry
from socialagent.templates.sync import sync_templates

__all__ = ["TemplateRegistry", "sync_templates"]
