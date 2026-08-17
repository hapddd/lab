"""Synthetic but realistically shaped ChatGPT payloads for the tests."""

from __future__ import annotations

import struct
import time
import zlib
from typing import Any, Dict, List, Optional

BASE_TIME = 1755000000.0  # 2025-08-12-ish, stable across runs


def png_bytes(width: int = 2, height: int = 2, rgb: tuple = (220, 30, 30)) -> bytes:
    """A real, decodable PNG so extension sniffing has something to chew on."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def jpeg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 32 + b"\xff\xd9"


def pdf_bytes() -> bytes:
    return b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _node(node_id: str, parent: Optional[str], children: List[str], message: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {"id": node_id, "parent": parent, "children": children, "message": message}


def _message(
    message_id: str,
    role: str,
    content: Dict[str, Any],
    offset: float = 0.0,
    metadata: Optional[Dict[str, Any]] = None,
    recipient: str = "all",
    name: Optional[str] = None,
) -> Dict[str, Any]:
    author: Dict[str, Any] = {"role": role, "metadata": {}}
    if name:
        author["name"] = name
    return {
        "id": message_id,
        "author": author,
        "create_time": BASE_TIME + offset,
        "update_time": BASE_TIME + offset,
        "content": content,
        "status": "finished_successfully",
        "end_turn": True,
        "weight": 1.0,
        "metadata": metadata or {},
        "recipient": recipient,
    }


IMAGE_FILE_ID = "file-Upload1234567890"
DALLE_FILE_ID = "file-Dalle0987654321"
PDF_FILE_ID = "file-Report5555555555"
PLOT_FILE_ID = "file-Plot7777777777"


def conversation_payload(
    conversation_id: str = "68a10000-1111-2222-3333-444455556666",
    title: str = "退出账号记录保存",
    with_branch: bool = True,
) -> Dict[str, Any]:
    """A conversation covering the content types the renderer must handle."""
    mapping: Dict[str, Any] = {}

    mapping["root"] = _node("root", None, ["n-sys"], None)
    mapping["n-sys"] = _node(
        "n-sys",
        "root",
        ["n-user-1"],
        _message(
            "m-sys",
            "system",
            {"content_type": "text", "parts": [""]},
            metadata={"is_visually_hidden_from_conversation": True},
        ),
    )

    # User turn with an uploaded screenshot (image pointer + attachment record).
    mapping["n-user-1"] = _node(
        "n-user-1",
        "n-sys",
        ["n-asst-1"],
        _message(
            "m-user-1",
            "user",
            {
                "content_type": "multimodal_text",
                "parts": [
                    {
                        "content_type": "image_asset_pointer",
                        "asset_pointer": f"file-service://{IMAGE_FILE_ID}",
                        "size_bytes": 84213,
                        "width": 1024,
                        "height": 640,
                    },
                    "请问 如果我退出了当前账号 这些记录还能看到吗",
                ],
            },
            offset=10,
            metadata={
                "attachments": [
                    {
                        "id": IMAGE_FILE_ID,
                        "name": "截图.png",
                        "mime_type": "image/png",
                        "size": 84213,
                        "width": 1024,
                        "height": 640,
                    },
                    {
                        "id": PDF_FILE_ID,
                        "name": "账号说明.pdf",
                        "mime_type": "application/pdf",
                        "size": 20480,
                    },
                ]
            },
        ),
    )

    # Assistant reply with reasoning, citations and private-use markers.
    mapping["n-asst-1"] = _node(
        "n-asst-1",
        "n-user-1",
        ["n-user-2"],
        _message(
            "m-asst-1",
            "assistant",
            {
                "content_type": "text",
                "parts": [
                    "可以。如果你只是\u201c退出登录\u201d，之后再登录回同一个 ChatGPT 账号，"
                    "你截图里这些聊天记录还会在。\ue200cite\ue202turn0search1\ue201\n\n"
                    "需要区分几种情况：**退出账号 ≠ 删除聊天 ≠ 删除账号**。【4:0†source】"
                ],
            },
            offset=20,
            metadata={
                "model_slug": "gpt-5",
                "citations": [
                    {
                        "start_ix": 10,
                        "end_ix": 20,
                        "citation_format_type": "tether_og",
                        "metadata": {
                            "type": "webpage",
                            "title": "OpenAI Help Center",
                            "url": "https://help.openai.com/en/articles/8265332",
                        },
                    }
                ],
                "content_references": [
                    {
                        "matched_text": "\ue200cite\ue202turn0search1\ue201",
                        "type": "webpage",
                        "title": "Data controls FAQ",
                        "url": "https://help.openai.com/en/articles/7730893",
                    }
                ],
            },
        ),
    )

    # Second user turn -> assistant generates an image, with a regenerated branch.
    mapping["n-user-2"] = _node(
        "n-user-2",
        "n-asst-1",
        ["n-asst-2a", "n-asst-2b"] if with_branch else ["n-asst-2b"],
        _message(
            "m-user-2",
            "user",
            {"content_type": "text", "parts": ["我要是登陆不同的账号呢，顺手画个示意图"]},
            offset=30,
        ),
    )

    if with_branch:
        mapping["n-asst-2a"] = _node(
            "n-asst-2a",
            "n-user-2",
            [],
            _message(
                "m-asst-2a",
                "assistant",
                {"content_type": "text", "parts": ["（这是被重新生成覆盖掉的旧回答）"]},
                offset=40,
                metadata={"model_slug": "gpt-5"},
            ),
        )

    mapping["n-asst-2b"] = _node(
        "n-asst-2b",
        "n-user-2",
        ["n-thoughts"],
        _message(
            "m-asst-2b",
            "assistant",
            {
                "content_type": "multimodal_text",
                "parts": [
                    {
                        "content_type": "image_asset_pointer",
                        "asset_pointer": f"sediment://{DALLE_FILE_ID}",
                        "size_bytes": 512000,
                        "width": 1024,
                        "height": 1024,
                        "metadata": {"dalle": {"prompt": "两个账号各自独立的聊天记录示意图", "gen_id": "gen-abc"}},
                    },
                    "换号 = 换一套聊天记录，不会在同一台电脑上合并。",
                ],
            },
            offset=50,
            metadata={"model_slug": "gpt-5"},
        ),
    )

    # Reasoning summary + a python tool call that produced a plot.
    mapping["n-thoughts"] = _node(
        "n-thoughts",
        "n-asst-2b",
        ["n-tool-call"],
        _message(
            "m-thoughts",
            "assistant",
            {
                "content_type": "thoughts",
                "thoughts": [{"summary": "核对账号隔离规则", "content": "退出登录只清除本地会话，服务端记录仍在。"}],
            },
            offset=55,
            metadata={"model_slug": "gpt-5"},
        ),
    )
    mapping["n-tool-call"] = _node(
        "n-tool-call",
        "n-thoughts",
        ["n-tool-out"],
        _message(
            "m-tool-call",
            "assistant",
            {"content_type": "code", "language": "python", "text": "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])"},
            offset=56,
            recipient="python",
            metadata={"model_slug": "gpt-5"},
        ),
    )
    mapping["n-tool-out"] = _node(
        "n-tool-out",
        "n-tool-call",
        [],
        _message(
            "m-tool-out",
            "tool",
            {"content_type": "execution_output", "text": "[<matplotlib.lines.Line2D object>]"},
            offset=57,
            name="python",
            metadata={
                "aggregate_result": {
                    "messages": [
                        {"message_type": "image", "image_url": f"file-service://{PLOT_FILE_ID}"}
                    ]
                }
            },
        ),
    )

    return {
        "title": title,
        "create_time": BASE_TIME,
        "update_time": BASE_TIME + 60,
        "mapping": mapping,
        "moderation_results": [],
        "current_node": "n-tool-out",
        "conversation_id": conversation_id,
        "is_archived": False,
        "default_model_slug": "gpt-5",
    }


def second_conversation_payload() -> Dict[str, Any]:
    mapping = {
        "root": _node("root", None, ["n1"], None),
        "n1": _node(
            "n1",
            "root",
            ["n2"],
            _message("m1", "user", {"content_type": "text", "parts": ["配置 Clash Verge 多订阅合并"]}, offset=1),
        ),
        "n2": _node(
            "n2",
            "n1",
            [],
            _message(
                "m2",
                "assistant",
                {
                    "content_type": "code",
                    "language": "yaml",
                    "text": "proxy-providers:\n  sub1:\n    url: https://example.com/sub\n",
                },
                offset=2,
                metadata={"model_slug": "gpt-5-thinking"},
            ),
        ),
    }
    return {
        "title": "配置 Clash Verge 多订阅合并",
        "create_time": BASE_TIME - 86400,
        "update_time": BASE_TIME - 80000,
        "mapping": mapping,
        "current_node": "n2",
        "conversation_id": "68a20000-aaaa-bbbb-cccc-ddddeeeeffff",
        "is_archived": False,
        "default_model_slug": "gpt-5-thinking",
    }


def archived_conversation_payload() -> Dict[str, Any]:
    payload = second_conversation_payload()
    payload["conversation_id"] = "68a30000-9999-8888-7777-666655554444"
    payload["title"] = "已归档的旧对话"
    payload["is_archived"] = True
    payload["update_time"] = BASE_TIME - 900000
    return payload


FILE_BLOBS: Dict[str, tuple] = {
    IMAGE_FILE_ID: (png_bytes(4, 4, (10, 120, 240)), "image/png", "截图.png"),
    DALLE_FILE_ID: (png_bytes(6, 6, (240, 180, 20)), "image/webp", "示意图.webp"),
    PDF_FILE_ID: (pdf_bytes(), "application/pdf", "账号说明.pdf"),
    PLOT_FILE_ID: (png_bytes(8, 2, (30, 30, 30)), "image/png", "plot.png"),
}


def conversation_list_items(payloads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": payload["conversation_id"],
            "title": payload["title"],
            "create_time": payload["create_time"],
            "update_time": payload["update_time"],
            "mapping": None,
            "current_node": None,
            "is_archived": payload.get("is_archived", False),
        }
        for payload in payloads
    ]


def now() -> float:
    return time.time()
