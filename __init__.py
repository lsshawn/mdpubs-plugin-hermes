"""mdpubs Hermes plugin — package entry point."""

from __future__ import annotations


def register(ctx) -> None:
    """Register the plugin's hooks. Hermes calls this with a context object
    exposing `register_hook(name, fn)`."""
    from .plugin import on_transform_llm_output

    ctx.register_hook("transform_llm_output", on_transform_llm_output)
