"""
Agent Loop
==========
Generic tool-calling loop using the Anthropic tool use API.

Agents call run_with_tools() instead of llm_complete() when they need to
execute production tools (inspect media, call scripts, etc.) during their
reasoning process.

The loop:
  1. Send messages to the model with available tool schemas
  2. Model returns tool_use blocks → execute via call_tool()
  3. Send tool_result blocks back → repeat until stop_reason == "end_turn"
  4. Return final text + all tool calls made

Falls back to llm_complete() (which handles CLI fallback) when:
  - No tool_names provided (empty list)
  - No ANTHROPIC_API_KEY available
"""

import inspect
import json
import types
from typing import Optional, Union, get_origin, get_args


def _annotation_to_json_schema(annotation) -> dict:
    """Convert a Python type annotation to a JSON Schema dict."""
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Handle Optional[X] = Union[X, None] and X | None (Python 3.10+)
    if origin is Union and type(None) in args:
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return _annotation_to_json_schema(non_none[0])
        return {"type": "string"}

    # Python 3.10+ union syntax: X | Y
    if isinstance(annotation, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return _annotation_to_json_schema(non_none[0])
        return {"type": "string"}

    if annotation == str:
        return {"type": "string"}
    if annotation == int:
        return {"type": "integer"}
    if annotation == float:
        return {"type": "number"}
    if annotation == bool:
        return {"type": "boolean"}
    if annotation == list or origin is list:
        if args:
            return {"type": "array", "items": _annotation_to_json_schema(args[0])}
        return {"type": "array", "items": {"type": "string"}}
    if annotation == dict or origin is dict:
        return {"type": "object"}

    # Unknown → string
    return {"type": "string"}


def _is_optional(annotation) -> bool:
    """Return True if the annotation is Optional[X] or X | None."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Union and type(None) in args:
        return True
    if isinstance(annotation, types.UnionType) and type(None) in args:
        return True
    return False


def build_tool_schemas(tool_names: list[str]) -> list[dict]:
    """
    Convert TOOL_REGISTRY function signatures to Anthropic tool input schemas.

    Args:
        tool_names: list of tool names from TOOL_REGISTRY

    Returns:
        list of Anthropic tool dicts {name, description, input_schema}
    """
    from packs.video.tools import TOOL_REGISTRY

    schemas = []
    for name in tool_names:
        fn = TOOL_REGISTRY.get(name)
        if not fn:
            continue

        sig = inspect.signature(fn)
        props = {}
        required = []

        for pname, param in sig.parameters.items():
            annotation = param.annotation
            schema = _annotation_to_json_schema(annotation)

            # Extract first-line description from docstring if available
            # (We don't parse per-param docs — just use the type schema)
            props[pname] = schema

            # Required if: has no default AND annotation is not Optional
            has_default = param.default is not inspect.Parameter.empty
            is_opt = annotation is inspect.Parameter.empty or _is_optional(annotation)
            if not has_default and not is_opt:
                required.append(pname)

        # First sentence of docstring as tool description
        doc = (fn.__doc__ or "").strip()
        description = doc.split("\n")[0].strip() if doc else name

        schemas.append({
            "name": name,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        })

    return schemas


def run_with_tools(
    model: str,
    system: str,
    prompt: str,
    tool_names: list[str],
    max_tokens: int = 4096,
    max_turns: int = 10,
    cost_context: Optional[dict] = None,
) -> dict:
    """
    Run a model call with tool use support.

    If tool_names is empty or no ANTHROPIC_API_KEY is available, falls back to
    llm_complete() (which also tries the claude CLI). This ensures the same
    behavior as before when tools aren't needed.

    Args:
        model: model name (e.g. "claude-sonnet-4-6")
        system: system prompt
        prompt: user prompt
        tool_names: list of tool names from TOOL_REGISTRY to expose
        max_tokens: max tokens per response turn
        max_turns: maximum tool call turns before forcing end
        cost_context: {concept_id, agent, run_id} for cost logging

    Returns:
        {text, tool_calls, input_tokens, output_tokens}
        tool_calls = [{tool, args, result}, ...]
    """
    import os
    from packs.video.tools import call_tool

    # If no tools requested or no API key — fall back to single llm_complete call
    if not tool_names or not os.environ.get("ANTHROPIC_API_KEY"):
        from core.llm import complete as llm_complete
        try:
            response = llm_complete(model=model, system=system, prompt=prompt, max_tokens=max_tokens)
        except Exception as e:
            raise RuntimeError(f"[agent_loop] LLM call failed: {e}") from e
        return {**response, "tool_calls": []}

    import anthropic
    from core.cost_tracker import log_call

    client = anthropic.Anthropic()
    tool_schemas = build_tool_schemas(tool_names)

    messages = [{"role": "user", "content": prompt}]
    all_tool_calls = []
    total_input = 0
    total_output = 0

    response = None
    for _ in range(max_turns):
        create_kwargs: dict = {
            "model": model,
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tool_schemas:
            create_kwargs["tools"] = tool_schemas
        response = client.messages.create(**create_kwargs)
        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens

        # Log cost each turn
        if cost_context:
            try:
                log_call(
                    concept_id=cost_context.get("concept_id", "UNKNOWN"),
                    agent=cost_context.get("agent", "agent_loop"),
                    run_id=cost_context.get("run_id", "unknown"),
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    model=model,
                )
            except Exception as e:
                print(f"[agent_loop] WARNING: cost logging failed: {e}")

        # Extract text and tool_use blocks from response
        text_parts = []
        tool_use_blocks = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)  # type: ignore[union-attr]
            elif block.type == "tool_use":
                tool_use_blocks.append(block)

        # No tool calls or model is done → return
        if not tool_use_blocks or response.stop_reason == "end_turn":
            return {
                "text": "\n".join(text_parts),
                "tool_calls": all_tool_calls,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "backend": "api",
            }

        # Execute each tool call and collect results
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in tool_use_blocks:
            tool_name = block.name
            tool_args = block.input

            arg_preview = ", ".join(f"{k}={repr(v)[:40]}" for k, v in tool_args.items())
            agent_label = cost_context.get("agent", "?") if cost_context else "?"
            print(f"  [agent/{agent_label}] → {tool_name}({arg_preview})")

            try:
                tool_result = call_tool(tool_name, **tool_args)
            except Exception as e:
                tool_result = {"success": False, "error": str(e), "tool": tool_name}

            all_tool_calls.append({"tool": tool_name, "args": tool_args, "result": tool_result})

            # Format result content for the API — prefer stdout, then error, then JSON
            result_content = (
                tool_result.get("stdout")
                or tool_result.get("error")
                or (f"Output: {tool_result['output_path']}" if tool_result.get("output_path") else None)
                or json.dumps({k: v for k, v in tool_result.items() if k != "stdout"})
            )
            if not result_content:
                result_content = "Done."

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_content[:3000],  # avoid context bloat
            })

        messages.append({"role": "user", "content": tool_results})  # type: ignore[arg-type]

    # Max turns hit — return whatever text we have from the last response
    last_text = ""
    if response is not None:
        last_text = "\n".join(
            block.text  # type: ignore[union-attr]
            for block in response.content
            if block.type == "text"
        )
    return {
        "text": last_text or "[agent_loop] max turns reached",
        "tool_calls": all_tool_calls,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "backend": "api",
    }
