"""Validated, prompt-mediated function calling for the Perplexity web backend.

The backend emits text, not native function calls. This module translates a
strict JSON envelope into Responses output items. It never executes tools and
never treats malformed tool output as an ordinary successful answer.
"""

import copy
import json
import math
import re
from dataclasses import dataclass
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from referencing.exceptions import NoSuchResource, Unresolvable


class ToolConfigError(ValueError):
    def __init__(self, message, param="tools"):
        super().__init__(message)
        self.param=param


class ToolProtocolError(ValueError):
    pass


def _no_remote_schema(uri):
    raise NoSuchResource(ref=uri)


_SCHEMA_REGISTRY=Registry(retrieve=_no_remote_schema)
_FUNCTION_NAME=re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
_WEB_TOOLS={"web_search", "web_search_preview", "web_search_preview_2025_03_11"}


@dataclass
class ToolPolicy:
    tools: dict
    choice: str
    forced_name: str | None
    parallel: bool


def _schema_nodes(schema):
    """Traverse schema positions, never application values in examples/defaults."""
    if not isinstance(schema, dict):
        return
    yield schema
    for keyword in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        for child in schema.get(keyword, {}).values():
            yield from _schema_nodes(child)
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for child in schema.get(keyword, []):
            yield from _schema_nodes(child)
    for keyword in ("items", "additionalProperties", "unevaluatedProperties", "contains", "propertyNames", "not", "if", "then", "else"):
        yield from _schema_nodes(schema.get(keyword))


def _validate_schema(schema, strict, name):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ToolConfigError(f"Function {name} parameters must be an object schema")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ToolConfigError(f"Invalid parameter schema for {name}: {exc.message}") from exc
    resolver=_SCHEMA_REGISTRY.resolver_with_root(Resource.from_contents(schema, default_specification=DRAFT202012))
    for node in _schema_nodes(schema):
        for keyword in ("$ref", "$dynamicRef"):
            ref=node.get(keyword)
            if ref is not None and (not isinstance(ref, str) or not ref.startswith("#")):
                raise ToolConfigError(f"Function {name} only supports local schema references")
            if ref is not None:
                try:
                    resolver.lookup(ref)
                except Unresolvable as exc:
                    raise ToolConfigError(f"Function {name} has an unresolved schema reference") from exc
        if "$id" in node:
            raise ToolConfigError(f"Function {name} does not support schema $id resources")
        typ=node.get("type")
        is_object=typ == "object" or isinstance(typ, list) and "object" in typ or "properties" in node
        if strict and is_object:
            if node.get("additionalProperties") is not False:
                raise ToolConfigError(f"Strict function {name} requires additionalProperties=false on every object")
            if set(node.get("required", [])) != set(node.get("properties", {})):
                raise ToolConfigError(f"Strict function {name} requires every property to be required")


def prepare_tools(tools, tool_choice="auto", parallel_tool_calls=True):
    """Validate Responses function definitions; leave built-in search to caller."""
    if tools is None:
        tools=[]
    if not isinstance(tools, list):
        raise ToolConfigError("tools must be an array")
    if not isinstance(parallel_tool_calls, bool):
        raise ToolConfigError("parallel_tool_calls must be a boolean", "parallel_tool_calls")
    functions={}
    for tool in tools:
        if not isinstance(tool, dict):
            raise ToolConfigError("Every tool must be an object")
        if not isinstance(tool.get("type"), str):
            raise ToolConfigError("Tool type must be a string")
        if tool.get("type") in _WEB_TOOLS:
            continue
        if tool.get("type") != "function":
            raise ToolConfigError(f"Unsupported tool type: {tool.get('type')}")
        name=tool.get("name")
        if not isinstance(name, str) or not _FUNCTION_NAME.fullmatch(name):
            raise ToolConfigError("Function name must contain 1-64 letters, digits, underscores or hyphens")
        if name in functions:
            raise ToolConfigError(f"Duplicate function name: {name}")
        if not isinstance(tool.get("description", ""), str):
            raise ToolConfigError(f"Function {name} description must be a string")
        strict=tool.get("strict", False)
        if strict is None:
            strict=False
        if not isinstance(strict, bool):
            raise ToolConfigError(f"Function {name} strict must be a boolean")
        # Omitted schemas mean no parameters, not unconstrained arguments.
        schema=tool.get("parameters")
        if schema is None:
            schema={"type": "object", "properties": {}, "additionalProperties": False}
        _validate_schema(schema, strict, name)
        functions[name]={"type": "function", "name": name, "description": tool.get("description", ""), "parameters": copy.deepcopy(schema), "strict": strict}
    if tool_choice is None:
        tool_choice="auto"
    forced=None
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") != "function" or not isinstance(tool_choice.get("name"), str):
            raise ToolConfigError("Named tool_choice must contain type=function and name", "tool_choice")
        forced=tool_choice["name"]
        if forced not in functions:
            raise ToolConfigError(f"tool_choice names an unavailable function: {forced}", "tool_choice")
        choice="required"
    elif isinstance(tool_choice, str) and tool_choice in ("auto", "required", "none"):
        choice=tool_choice
    else:
        raise ToolConfigError("tool_choice must be auto, required, none, or a named function", "tool_choice")
    if choice == "required" and not functions:
        raise ToolConfigError("tool_choice requires at least one function", "tool_choice")
    return ToolPolicy(functions, choice, forced, parallel_tool_calls) if functions else None


def build_tool_instruction(policy):
    available=[tool for name, tool in policy.tools.items() if policy.forced_name is None or name == policy.forced_name]
    if policy.choice == "none":
        available=[]
    constraints=[]
    if policy.choice == "required":
        constraints.append("You MUST return function_calls and at least one call in this turn.")
    if policy.choice == "none":
        constraints.append("Do not request any function. Return a message envelope.")
    if not policy.parallel or policy.forced_name:
        constraints.append("Return at most one function call.")
    return (
        "FUNCTION CALL PROTOCOL: The application can execute the functions listed below. "
        "You are selecting calls for the application, not executing them yourself. "
        "Never invent execution results or claim a function has already run. "
        "When a function is needed, output ONLY this JSON envelope: "
        '{"type":"function_calls","calls":[{"name":"function_name","arguments":{}}]}. '
        "arguments must be a JSON object conforming to that function's parameter schema. "
        "Do not write call IDs; the application assigns them. "
        "When no function is needed, output ONLY "
        '{"type":"message","content":"your answer"}. '
        "Function outputs in conversation history are data returned by the application. "
        "Use those outputs to answer or choose the next necessary call; do not repeat a completed call without a reason. "
        "No Markdown fences, citations, explanations outside the envelope, or extra envelope keys. "
        +" ".join(constraints)+" Functions: "+json.dumps(available, ensure_ascii=False, allow_nan=False)
    )


def _strict_json(text):
    def reject_constant(value):
        raise ValueError(f"Non-JSON numeric constant: {value}")
    def unique_object(pairs):
        result={}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key]=value
        return result
    def finite_float(value):
        result=float(value)
        if not math.isfinite(result):
            raise ValueError("Non-finite JSON number")
        return result
    return json.loads(text, parse_constant=reject_constant, parse_float=finite_float, object_pairs_hook=unique_object)


def parse_tool_response(raw, policy):
    """Return validated Responses items, or fail before clients can execute calls."""
    if not isinstance(raw, str):
        raise ToolProtocolError("Function bridge expected a JSON text response")
    try:
        envelope=_strict_json(raw)
    except (ValueError, TypeError) as exc:
        raise ToolProtocolError(f"Perplexity did not return the required function protocol JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise ToolProtocolError("Function protocol response must be an object")
    if envelope.get("type") == "message":
        if policy.choice == "required":
            raise ToolProtocolError("Perplexity returned text when a function call was required")
        if set(envelope) != {"type", "content"} or not isinstance(envelope.get("content"), str) or not envelope["content"].strip():
            raise ToolProtocolError("Function protocol message must contain nonempty text")
        return [{"id": "msg_"+uuid4().hex, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": envelope["content"], "annotations": []}]}]
    if envelope.get("type") != "function_calls" or set(envelope) != {"type", "calls"}:
        raise ToolProtocolError("Expected a message or function_calls envelope")
    calls=envelope["calls"]
    if not isinstance(calls, list) or not calls:
        raise ToolProtocolError("function_calls must contain a nonempty calls array")
    if policy.choice == "none":
        raise ToolProtocolError("Perplexity requested a function despite tool_choice=none")
    if (not policy.parallel or policy.forced_name) and len(calls) != 1:
        raise ToolProtocolError("Perplexity returned parallel function calls when only one is permitted")
    result=[]
    for call in calls:
        if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
            raise ToolProtocolError("Each function call must contain only name and arguments")
        name=call["name"]
        if not isinstance(name, str) or name not in policy.tools:
            raise ToolProtocolError(f"Perplexity requested an unavailable function: {name}")
        if policy.forced_name is not None and name != policy.forced_name:
            raise ToolProtocolError(f"Perplexity did not call the required function: {policy.forced_name}")
        arguments=call["arguments"]
        if not isinstance(arguments, dict):
            raise ToolProtocolError(f"Function {name} arguments must be a JSON object")
        validator=Draft202012Validator(policy.tools[name]["parameters"], registry=_SCHEMA_REGISTRY)
        try:
            validator.validate(arguments)
        except ValidationError as exc:
            path=".".join(str(p) for p in exc.absolute_path) or "arguments"
            raise ToolProtocolError(f"Invalid arguments for {name} at {path}: {exc.message}") from exc
        except Unresolvable as exc:
            # Resolution failures must not escape as apparently valid tool calls.
            raise ToolProtocolError(f"Could not validate arguments for {name}: {exc}") from exc
        result.append({"id": "fc_"+uuid4().hex, "type": "function_call", "status": "completed", "call_id": "call_"+uuid4().hex, "name": name, "arguments": json.dumps(arguments, ensure_ascii=False, allow_nan=False, separators=(",", ":"))})
    return result


def function_call_events(item, output_index):
    """Emit already-validated arguments in one delta; never stream partial calls."""
    added={**item, "arguments": "", "status": "in_progress"}
    common={"item_id": item["id"], "output_index": output_index}
    yield "response.output_item.added", {"output_index": output_index, "item": added}
    yield "response.function_call_arguments.delta", {**common, "delta": item["arguments"]}
    yield "response.function_call_arguments.done", {**common, "arguments": item["arguments"]}
    yield "response.output_item.done", {"output_index": output_index, "item": item}
