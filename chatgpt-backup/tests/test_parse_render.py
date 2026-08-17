import unittest

from chatgpt_backup import model
from chatgpt_backup.parse import ParseOptions, clean_text, linearize, parse_conversation, pointer_file_id
from chatgpt_backup.render import RenderOptions, render_conversation

from . import fixtures


class TestCleanText(unittest.TestCase):
    def test_strips_private_use_citation_markers(self):
        raw = "记录还会在。\ue200cite\ue202turn0search1\ue201 结束"
        self.assertEqual(clean_text(raw), "记录还会在。 结束")

    def test_strips_bracket_and_oaicite_markers(self):
        self.assertEqual(clean_text("答案【4:0†source】完毕"), "答案完毕")
        self.assertEqual(clean_text("答案 [oaicite:12]完毕"), "答案完毕")

    def test_collapses_excess_blank_lines(self):
        self.assertEqual(clean_text("a\n\n\n\n\nb"), "a\n\nb")

    def test_handles_none_and_non_strings(self):
        self.assertEqual(clean_text(None), "")
        self.assertEqual(clean_text(42), "42")


class TestPointerParsing(unittest.TestCase):
    def test_file_service_pointer(self):
        self.assertEqual(pointer_file_id("file-service://file-Abc123"), "file-Abc123")

    def test_sediment_pointer(self):
        self.assertEqual(pointer_file_id("sediment://file_00000000abc"), "file_00000000abc")

    def test_empty_pointer(self):
        self.assertIsNone(pointer_file_id(None))
        self.assertIsNone(pointer_file_id(""))


class TestLinearize(unittest.TestCase):
    def setUp(self):
        self.payload = fixtures.conversation_payload()
        self.mapping = self.payload["mapping"]

    def test_active_branch_only(self):
        nodes = linearize(self.mapping, self.payload["current_node"])
        ids = [node["id"] for node in nodes]
        self.assertIn("n-asst-2b", ids)
        self.assertNotIn("n-asst-2a", ids, "被重新生成覆盖的分支不应出现")

    def test_all_branches_includes_regenerated(self):
        nodes = linearize(self.mapping, self.payload["current_node"], all_branches=True)
        ids = [node["id"] for node in nodes]
        self.assertIn("n-asst-2a", ids)
        self.assertIn("n-asst-2b", ids)

    def test_missing_current_node_falls_back(self):
        nodes = linearize(self.mapping, "does-not-exist")
        self.assertTrue(nodes)

    def test_cycle_does_not_hang(self):
        mapping = {
            "a": {"id": "a", "parent": "b", "children": ["b"], "message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["x"]}}},
            "b": {"id": "b", "parent": "a", "children": ["a"], "message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["y"]}}},
        }
        nodes = linearize(mapping, "a")
        self.assertEqual(len(nodes), 2)


class TestParseConversation(unittest.TestCase):
    def setUp(self):
        self.payload = fixtures.conversation_payload()
        self.conversation = parse_conversation(self.payload, ParseOptions())

    def test_metadata(self):
        self.assertEqual(self.conversation.title, "退出账号记录保存")
        self.assertEqual(self.conversation.id, self.payload["conversation_id"])
        self.assertEqual(self.conversation.model, "gpt-5")
        self.assertTrue(self.conversation.url.startswith("https://chatgpt.com/c/"))
        self.assertIsNotNone(self.conversation.create_time)
        self.assertIsNotNone(self.conversation.update_time)

    def test_hidden_system_message_dropped_by_default(self):
        roles = [message.role for message in self.conversation.messages]
        self.assertNotIn("system", roles)

    def test_tool_messages_hidden_by_default(self):
        roles = [message.role for message in self.conversation.messages]
        self.assertNotIn("tool", roles)

    def test_tool_messages_included_on_request(self):
        conversation = parse_conversation(self.payload, ParseOptions(include_tools=True))
        roles = [message.role for message in conversation.messages]
        self.assertIn("tool", roles)
        kinds = [block.kind for message in conversation.messages for block in message.blocks]
        self.assertIn(model.TOOL_OUTPUT, kinds)
        self.assertIn(model.CODE, kinds)

    def test_user_image_and_attachment_assets(self):
        user_message = next(m for m in self.conversation.messages if m.role == "user")
        file_ids = {asset.file_id for asset in user_message.assets}
        self.assertIn(fixtures.IMAGE_FILE_ID, file_ids)
        self.assertIn(fixtures.PDF_FILE_ID, file_ids)
        image = next(a for a in user_message.assets if a.file_id == fixtures.IMAGE_FILE_ID)
        self.assertEqual(image.kind, model.IMAGE)
        self.assertEqual(image.width, 1024)
        pdf = next(a for a in user_message.assets if a.file_id == fixtures.PDF_FILE_ID)
        self.assertEqual(pdf.kind, model.FILE)
        self.assertEqual(pdf.name, "账号说明.pdf")

    def test_no_duplicate_asset_for_image_listed_twice(self):
        user_message = next(m for m in self.conversation.messages if m.role == "user")
        ids = [asset.file_id for asset in user_message.assets]
        self.assertEqual(len(ids), len(set(ids)))

    def test_generated_image_keeps_prompt(self):
        assets = {asset.file_id: asset for asset in self.conversation.assets}
        dalle = assets[fixtures.DALLE_FILE_ID]
        self.assertEqual(dalle.kind, model.IMAGE)
        self.assertEqual(dalle.prompt, "两个账号各自独立的聊天记录示意图")

    def test_reasoning_block_present_and_optional(self):
        kinds = [block.kind for message in self.conversation.messages for block in message.blocks]
        self.assertIn(model.THOUGHT, kinds)
        without = parse_conversation(self.payload, ParseOptions(include_thoughts=False))
        kinds = [block.kind for message in without.messages for block in message.blocks]
        self.assertNotIn(model.THOUGHT, kinds)

    def test_citations_collected_as_sources(self):
        assistant = next(m for m in self.conversation.messages if m.role == "assistant")
        urls = {item["url"] for item in assistant.sources}
        self.assertIn("https://help.openai.com/en/articles/8265332", urls)
        self.assertIn("https://help.openai.com/en/articles/7730893", urls)

    def test_malformed_payload_does_not_raise(self):
        conversation = parse_conversation({"title": None, "mapping": {"x": None}}, ParseOptions())
        self.assertEqual(conversation.title, "未命名对话")
        self.assertEqual(conversation.messages, [])


class TestRender(unittest.TestCase):
    def setUp(self):
        payload = fixtures.conversation_payload()
        self.conversation = parse_conversation(payload, ParseOptions(include_tools=True))
        for index, asset in enumerate(self.conversation.assets):
            asset.rel_path = f"assets/img-{index}.png"
        self.markdown = render_conversation(self.conversation, RenderOptions())

    def test_frontmatter_present_and_quoted(self):
        self.assertTrue(self.markdown.startswith("---\n"))
        self.assertIn('title: "退出账号记录保存"', self.markdown)
        self.assertIn("conversation_id:", self.markdown)
        self.assertIn("message_count:", self.markdown)

    def test_title_and_role_headings(self):
        self.assertIn("# 退出账号记录保存", self.markdown)
        self.assertIn("## 我", self.markdown)
        self.assertIn("## ChatGPT · gpt-5", self.markdown)

    def test_images_embedded_as_markdown(self):
        self.assertIn("![", self.markdown)
        self.assertIn("](assets/img-0.png)", self.markdown)

    def test_generated_image_prompt_rendered(self):
        self.assertIn("生成提示: 两个账号各自独立的聊天记录示意图", self.markdown)

    def test_code_block_has_language(self):
        self.assertIn("```python", self.markdown)

    def test_sources_section(self):
        self.assertIn("**参考来源**", self.markdown)
        self.assertIn("https://help.openai.com/en/articles/8265332", self.markdown)

    def test_no_private_use_characters_leak(self):
        self.assertNotIn("\ue200", self.markdown)
        self.assertNotIn("\ue202", self.markdown)

    def test_reasoning_in_collapsible_block(self):
        self.assertIn("<details>", self.markdown)
        self.assertIn("推理过程", self.markdown)

    def test_yaml_quotes_escaped(self):
        payload = fixtures.conversation_payload(title='他说 "退出" 之后')
        conversation = parse_conversation(payload, ParseOptions())
        markdown = render_conversation(conversation)
        self.assertIn('title: "他说 \\"退出\\" 之后"', markdown)

    def test_code_fence_longer_than_inner_backticks(self):
        conversation = parse_conversation(
            {
                "title": "t",
                "conversation_id": "c",
                "current_node": "n1",
                "mapping": {
                    "n1": {
                        "id": "n1",
                        "parent": None,
                        "children": [],
                        "message": {
                            "id": "m1",
                            "author": {"role": "assistant"},
                            "content": {"content_type": "code", "language": "md", "text": "```\nnested\n```"},
                            "metadata": {},
                        },
                    }
                },
            },
            ParseOptions(),
        )
        markdown = render_conversation(conversation)
        self.assertIn("````md", markdown)

    def test_missing_asset_is_reported_not_silently_dropped(self):
        for asset in self.conversation.assets:
            asset.rel_path = None
            asset.failed = True
        markdown = render_conversation(self.conversation)
        self.assertIn("附件下载失败", markdown)


if __name__ == "__main__":
    unittest.main()
