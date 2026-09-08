import copy
import json
import unittest

from function_tools import (
    ToolConfigError, ToolProtocolError, build_tool_instruction,
    function_call_events, parse_tool_response, prepare_tools,
)


class FunctionToolsTests(unittest.TestCase):
    def setUp(self):
        self.tool={
            "type": "function", "name": "lookup", "description": "Look up a record",
            "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"], "additionalProperties": False},
            "strict": True,
        }

    def envelope(self, calls):
        return json.dumps({"type": "function_calls", "calls": calls})

    def call(self, name="lookup", arguments=None):
        return {"name": name, "arguments": arguments if arguments is not None else {"key": "a"}}

    def test_unresolved_reference_fails_before_upstream(self):
        tool=copy.deepcopy(self.tool)
        tool['parameters']['properties']['key']={'$ref':'#/$defs/missing'}
        with self.assertRaises(ToolConfigError):
            prepare_tools([tool])

    def test_overflow_number_is_protocol_error(self):
        with self.assertRaises(ToolProtocolError):
            parse_tool_response('{"type":"function_calls","calls":[{"name":"lookup","arguments":{"key":1e999}}]}',prepare_tools([self.tool]))

    def test_valid_call_preserves_argument_strings_and_client_execution_boundary(self):
        policy=prepare_tools([self.tool])
        text="literal [1] <script>code</script> \\u0000 雪"
        item=parse_tool_response(self.envelope([self.call(arguments={"key": text})]), policy)[0]
        self.assertEqual(item["type"], "function_call")
        self.assertTrue(item["id"].startswith("fc_"))
        self.assertTrue(item["call_id"].startswith("call_"))
        self.assertNotEqual(item["id"], item["call_id"])
        self.assertEqual(json.loads(item["arguments"]), {"key": text})

    def test_parallel_calls_have_unique_ids(self):
        policy=prepare_tools([self.tool])
        items=parse_tool_response(self.envelope([self.call(), self.call(arguments={"key": "b"})]), policy)
        self.assertEqual(len({item["call_id"] for item in items}), 2)

    def test_nonparallel_and_named_choices_enforce_one_call(self):
        for choice, parallel in (("auto", False), ({"type": "function", "name": "lookup"}, True)):
            with self.subTest(choice=choice), self.assertRaises(ToolProtocolError):
                parse_tool_response(self.envelope([self.call(), self.call()]), prepare_tools([self.tool], choice, parallel))

    def test_required_cannot_turn_into_text_fallback(self):
        with self.assertRaises(ToolProtocolError):
            parse_tool_response('{"type":"message","content":"I cannot use tools"}', prepare_tools([self.tool], "required"))

    def test_none_cannot_emit_function_calls(self):
        policy=prepare_tools([self.tool], "none")
        with self.assertRaises(ToolProtocolError):
            parse_tool_response(self.envelope([self.call()]), policy)
        self.assertNotIn('"name": "lookup"', build_tool_instruction(policy))
        item=parse_tool_response('{"type":"message","content":"answer"}', policy)[0]
        self.assertEqual(item["content"][0]["text"], "answer")

    def test_named_choice_blocks_other_functions(self):
        other={**self.tool, "name": "other"}
        policy=prepare_tools([self.tool, other], {"type": "function", "name": "lookup"})
        with self.assertRaises(ToolProtocolError):
            parse_tool_response(self.envelope([self.call("other")]), policy)

    def test_schema_rejects_invalid_and_extra_arguments(self):
        policy=prepare_tools([self.tool])
        for arguments in ({}, {"key": 3}, {"key": "a", "extra": True}, [], '{"key":"a"}'):
            with self.subTest(arguments=arguments), self.assertRaises(ToolProtocolError):
                parse_tool_response(self.envelope([self.call(arguments=arguments)]), policy)

    def test_malformed_protocol_never_becomes_message(self):
        policy=prepare_tools([self.tool])
        invalid=["ordinary text", '```json\n{"type":"message","content":"ok"}\n```', "[]", "{}",
            '{"type":"function_calls","calls":[]}', '{"type":"message","content":""}',
            '{"type":"message","content":"ok","extra":true}',
            '{"type":"message","content":"first","content":"second"}',
            '{"type":"function_calls","calls":[{"name":"lookup","arguments":{"key":NaN}}]}',
            self.envelope([self.call("unknown")]), self.envelope([{"name": "lookup", "arguments": {}, "call_id": "forged"}])]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ToolProtocolError):
                parse_tool_response(raw, policy)

    def test_invalid_tool_configuration_fails_early(self):
        invalid=[None, "tool", {}, {**self.tool, "name": "has spaces"}, {**self.tool, "name": "x"*65},
            {**self.tool, "parameters": []}, {**self.tool, "parameters": {"type": "object", "properties": []}},
            {**self.tool, "strict": "true"}, {**self.tool, "description": []},
            {**self.tool, "parameters": {"type": "object", "properties": {"x": {"$ref": "https://example.com/schema"}}}},
            {**self.tool, "parameters": {"type": "object", "$id": "https://example.com"}}]
        for tool in invalid:
            with self.subTest(tool=tool), self.assertRaises(ToolConfigError):
                prepare_tools([tool])
        with self.assertRaises(ToolConfigError):
            prepare_tools([self.tool, self.tool])

    def test_strict_schema_constraints_are_checked_recursively(self):
        tool=copy.deepcopy(self.tool)
        tool["parameters"]["properties"]["key"]={"type": "object", "properties": {"x": {"type": "string"}}}
        with self.assertRaises(ToolConfigError):
            prepare_tools([tool])
        tool["parameters"]["properties"]["key"]["additionalProperties"]=False
        with self.assertRaises(ToolConfigError):
            prepare_tools([tool])

    def test_local_ref_schema_supported_and_never_fetches_remote_resources(self):
        tool=copy.deepcopy(self.tool)
        tool["parameters"]["$defs"]={"Key": {"type": "string", "pattern": "^[a-z]+$"}}
        tool["parameters"]["properties"]["key"]={"$ref": "#/$defs/Key"}
        policy=prepare_tools([tool])
        self.assertEqual(len(parse_tool_response(self.envelope([self.call()]), policy)), 1)
        with self.assertRaises(ToolProtocolError):
            parse_tool_response(self.envelope([self.call(arguments={"key": "123"})]), policy)

    def test_data_examples_are_not_treated_as_schema_resources(self):
        tool=copy.deepcopy(self.tool)
        tool["parameters"]["examples"]=[{"$ref": "https://example.com/data"}]
        self.assertIsNotNone(prepare_tools([tool]))

    def test_function_events_reconstruct_exact_arguments(self):
        item=parse_tool_response(self.envelope([self.call()]), prepare_tools([self.tool]))[0]
        events=list(function_call_events(item, 2))
        self.assertEqual([event for event, data in events], ["response.output_item.added", "response.function_call_arguments.delta", "response.function_call_arguments.done", "response.output_item.done"])
        self.assertEqual(events[0][1]["item"]["arguments"], "")
        self.assertEqual(events[1][1]["delta"], item["arguments"])
        self.assertEqual(events[2][1]["arguments"], item["arguments"])
        self.assertEqual(events[-1][1]["item"], item)
        self.assertTrue(all(data["output_index"] == 2 for event, data in events))

    def test_absent_function_definitions(self):
        self.assertIsNone(prepare_tools([]))
        self.assertIsNone(prepare_tools([{"type": "web_search"}]))
        with self.assertRaises(ToolConfigError):
            prepare_tools([], "required")
        with self.assertRaises(ToolConfigError):
            prepare_tools([], {"type": "function", "name": "missing"})


if __name__ == "__main__":
    unittest.main()
