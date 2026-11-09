import asyncio
import base64
from pathlib import Path

from eurus.agent import Agent
from eurus.catalog import BUILTIN_PROVIDERS
from eurus.content import DocumentBlock, ImageBlock
from eurus.media.store import MediaStore
from eurus.models import Message, ModelRef
from eurus.providers.anthropic import AnthropicProvider
from eurus.providers.base import RetryPolicy
from eurus.providers.gemini import GeminiProvider
from eurus.providers.media_parts import (
    ANTHROPIC_INLINE_BYTES,
    GEMINI_IMAGE_MIMES,
    MediaStoreAssetResolver,
    ResolvedAsset,
    collect_block_assets,
    resolve_request_assets,
    strip_message_blocks,
)
from eurus.providers.model_capabilities import required_modalities_for_messages
from eurus.providers.openai_compat import OpenAICompatProvider
from eurus.providers.router import Router
from eurus.providers.testing import FakeTransport, RecordingSleep, openai_stream
from eurus.tools import ToolContext, ToolRegistry

PNG_BYTES = (
    b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a\x00\x00\x00\x0d\x49\x48\x44\x52"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90\x77\x53"
    b"\xde\x00\x00\x00\x0c\x49\x44\x41\x54\x78\x9c\x63\xf8\xcf\xc0\x00"
    b"\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00\x49\x45\x4e"
    b"\x44\xae\x42\x60\x82"
)
PNG_BYTES_ALT = PNG_BYTES + b"\x00"
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
GIF_BYTES = b"GIF89a\x01\x00\x01\x00\x00\x00\x00"
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF"

OPENAI_OK = {
    "choices": [{"message": {"role": "assistant", "content": "a cat"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}
ANTHROPIC_OK = {
    "content": [{"type": "text", "text": "a diagram"}],
    "usage": {"input_tokens": 5, "output_tokens": 2},
    "stop_reason": "end_turn",
}
GEMINI_OK = {
    "candidates": [{"content": {"parts": [{"text": "a chart"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
}


class StaticResolver:
    def __init__(self, assets: dict[str, ResolvedAsset]) -> None:
        self.assets = assets
        self.requested: list[str] = []

    def resolve(self, asset_id: str) -> ResolvedAsset | None:
        self.requested.append(asset_id)
        return self.assets.get(asset_id)


def make_store(tmp_path: Path) -> MediaStore:
    return MediaStore(database=tmp_path / "db.sqlite", root=tmp_path / "media")


def save_image(store: MediaStore, data: bytes, name: str = "photo.png") -> str:
    return store.save("s1", name, data).id


def openai_vision_router(
    transport: FakeTransport,
    resolver: MediaStoreAssetResolver | None,
    model: str = "gpt-4o",
    provider_name: str = "openai",
) -> Router:
    provider = OpenAICompatProvider(
        BUILTIN_PROVIDERS[provider_name],
        "k",
        transport,
        RetryPolicy(max_retries=0),
        RecordingSleep(),
        asset_resolver=resolver,
    )
    return Router(
        {provider_name: provider},
        [ModelRef.parse(f"{provider_name}/{model}")],
    )


def anthropic_vision_router(
    transport: FakeTransport,
    resolver: MediaStoreAssetResolver | None,
    model: str = "claude-sonnet-4",
) -> Router:
    provider = AnthropicProvider(
        BUILTIN_PROVIDERS["anthropic"],
        "k",
        transport,
        RetryPolicy(max_retries=0),
        RecordingSleep(),
        asset_resolver=resolver,
    )
    return Router({"anthropic": provider}, [ModelRef.parse(f"anthropic/{model}")])


def gemini_vision_router(
    transport: FakeTransport, resolver: MediaStoreAssetResolver | None, model: str = "gemini-2.0"
) -> Router:
    provider = GeminiProvider(
        BUILTIN_PROVIDERS["gemini"],
        "k",
        transport,
        RetryPolicy(max_retries=0),
        RecordingSleep(),
        asset_resolver=resolver,
    )
    return Router({"gemini": provider}, [ModelRef.parse(f"gemini/{model}")])


def run(coro):
    return asyncio.run(coro)


async def chat_ok(router: Router, message: Message) -> object:
    return await router.chat((Message.system("sys"), message))


def test_image_serialization_keeps_reference_not_binary(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    asset_id = save_image(store, PNG_BYTES)
    message = Message.user("what is this?", (ImageBlock(asset_id=asset_id, mime_type="image/png"),))
    data = message.to_dict()
    assert data["blocks"][0]["asset_id"] == asset_id
    assert base64.b64encode(PNG_BYTES).decode("ascii") not in str(data)


def test_attachment_resolution_reads_stored_asset(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    asset_id = save_image(store, PNG_BYTES)
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    message = Message.user("look", (ImageBlock(asset_id=asset_id, mime_type="image/png"),))
    assets = run(
        resolve_request_assets(
            (message,),
            resolver,
            allowed_mimes=frozenset({"image/png"}),
            inline_bytes={"image/": 1024 * 1024},
        )
    )
    assert asset_id in assets
    assert assets[asset_id].data == PNG_BYTES
    assert assets[asset_id].mime == "image/png"


def test_missing_asset_falls_back_to_text(tmp_path: Path) -> None:
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    message = Message.user("look", (ImageBlock(asset_id="a" * 32, mime_type="image/png"),))
    assets = run(
        resolve_request_assets(
            (message,),
            resolver,
            allowed_mimes=frozenset({"image/png"}),
            inline_bytes={"image/": 1024 * 1024},
        )
    )
    assert assets == {}
    transport = FakeTransport(json_responses=[OPENAI_OK])
    router = openai_vision_router(transport, resolver)
    run(chat_ok(router, message))
    wire = transport.calls[0].payload["messages"][1]
    assert wire["content"] == "look"


def test_mime_mismatch_is_rejected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    asset_id = store.save("s1", "photo.jpg", JPEG_BYTES).id
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    message = Message.user("look", (ImageBlock(asset_id=asset_id, mime_type="image/png"),))
    assets = run(
        resolve_request_assets(
            (message,),
            resolver,
            allowed_mimes=frozenset({"image/png", "image/jpeg"}),
            inline_bytes={"image/": 1024 * 1024},
        )
    )
    assert assets == {}


def test_oversized_image_is_not_rendered_inline(tmp_path: Path) -> None:
    resolver = StaticResolver(
        {"a" * 32: ResolvedAsset("a" * 32, "image/png", PNG_BYTES, "photo.png")}
    )
    message = Message.user("look", (ImageBlock(asset_id="a" * 32, mime_type="image/png"),))
    assets = run(
        resolve_request_assets(
            (message,),
            resolver,
            allowed_mimes=frozenset({"image/png"}),
            inline_bytes={"image/": 8},
        )
    )
    assert assets == {}


def test_resolver_unavailable_mime_is_not_requested() -> None:
    resolver = StaticResolver({})
    message = Message.user("look", (ImageBlock(asset_id="a" * 32, mime_type="image/bmp"),))
    assets = run(
        resolve_request_assets(
            (message,),
            resolver,
            allowed_mimes=frozenset({"image/png", "image/jpeg"}),
            inline_bytes={"image/": 1024 * 1024},
        )
    )
    assert assets == {}
    assert resolver.requested == []


def test_multiple_images_render_in_order(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = save_image(store, PNG_BYTES, "one.png")
    second = save_image(store, PNG_BYTES_ALT, "two.png")
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    message = Message.user(
        "compare these",
        (
            ImageBlock(asset_id=first, mime_type="image/png"),
            ImageBlock(asset_id=second, mime_type="image/png"),
        ),
    )
    transport = FakeTransport(json_responses=[OPENAI_OK])
    router = openai_vision_router(transport, resolver)
    run(chat_ok(router, message))
    content = transport.calls[0].payload["messages"][1]["content"]
    images = [part for part in content if part["type"] == "image_url"]
    assert len(images) == 2
    assert content[0] == {"type": "text", "text": "compare these"}
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64encode(PNG_BYTES).decode("ascii") in images[0]["image_url"]["url"]
    assert base64.b64encode(PNG_BYTES_ALT).decode("ascii") in images[1]["image_url"]["url"]


def test_gif_support_follows_provider_allowlist() -> None:
    gif_id = "b" * 32
    resolver = StaticResolver({gif_id: ResolvedAsset(gif_id, "image/gif", GIF_BYTES, "anim.gif")})
    message = Message.user("loop?", (ImageBlock(asset_id=gif_id, mime_type="image/gif"),))
    assets = run(
        resolve_request_assets(
            (message,),
            resolver,
            allowed_mimes=frozenset({"image/gif", "image/png"}),
            inline_bytes={"image/": 1024 * 1024},
        )
    )
    assert gif_id in assets
    gemini_assets = run(
        resolve_request_assets(
            (message,),
            resolver,
            allowed_mimes=GEMINI_IMAGE_MIMES,
            inline_bytes={"image/": 1024 * 1024},
        )
    )
    assert gemini_assets == {}


def test_anthropic_request_conversion(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    asset_id = save_image(store, PNG_BYTES)
    pdf_id = store.save("s1", "doc.pdf", PDF_BYTES).id
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    message = Message.user(
        "explain",
        (
            ImageBlock(asset_id=asset_id, mime_type="image/png"),
            DocumentBlock(asset_id=pdf_id, mime_type="application/pdf", filename="doc.pdf"),
        ),
    )
    transport = FakeTransport(json_responses=[ANTHROPIC_OK])
    router = anthropic_vision_router(transport, resolver)
    run(chat_ok(router, message))
    payload = transport.calls[0].payload
    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "explain"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"
    assert base64.b64encode(PNG_BYTES).decode("ascii") == content[1]["source"]["data"]
    assert content[2]["type"] == "document"
    assert content[2]["source"]["media_type"] == "application/pdf"


def test_gemini_request_conversion(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    asset_id = save_image(store, PNG_BYTES)
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    message = Message.user("jelaskan", (ImageBlock(asset_id=asset_id, mime_type="image/png"),))
    transport = FakeTransport(json_responses=[GEMINI_OK])
    router = gemini_vision_router(transport, resolver)
    run(chat_ok(router, message))
    contents = transport.calls[0].payload["contents"]
    user_parts = contents[0]["parts"]
    assert user_parts[0] == {"text": "jelaskan"}
    assert user_parts[1]["inline_data"]["mime_type"] == "image/png"
    assert user_parts[1]["inline_data"]["data"] == base64.b64encode(PNG_BYTES).decode("ascii")


def test_document_routes_to_document_capable_protocol(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    pdf_id = store.save("s1", "doc.pdf", PDF_BYTES).id
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    message = Message.user(
        "summarize", (DocumentBlock(asset_id=pdf_id, mime_type="application/pdf"),)
    )
    openai_transport = FakeTransport(json_responses=[OPENAI_OK])
    anthropic_transport = FakeTransport(json_responses=[ANTHROPIC_OK])
    openai_provider = OpenAICompatProvider(
        BUILTIN_PROVIDERS["openai"],
        "k",
        openai_transport,
        RetryPolicy(max_retries=0),
        RecordingSleep(),
        asset_resolver=resolver,
    )
    anthropic_provider = AnthropicProvider(
        BUILTIN_PROVIDERS["anthropic"],
        "k",
        anthropic_transport,
        RetryPolicy(max_retries=0),
        RecordingSleep(),
        asset_resolver=resolver,
    )
    router = Router(
        {"openai": openai_provider, "anthropic": anthropic_provider},
        [
            ModelRef.parse("openai/gpt-4o"),
            ModelRef.parse("anthropic/claude-sonnet-4"),
        ],
    )
    run(chat_ok(router, message))
    assert openai_transport.calls == []
    assert anthropic_transport.calls[0].url.endswith("/v1/messages")
    assert anthropic_transport.calls[0].payload["messages"][0]["content"][1]["type"] == "document"


def test_text_only_model_receives_text_bridge(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    asset_id = save_image(store, PNG_BYTES)
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    message = Message.user(
        "what is in this image?", (ImageBlock(asset_id=asset_id, mime_type="image/png"),)
    )
    transport = FakeTransport(json_responses=[OPENAI_OK])
    provider = OpenAICompatProvider(
        BUILTIN_PROVIDERS["groq"],
        "k",
        transport,
        RetryPolicy(max_retries=0),
        RecordingSleep(),
        asset_resolver=resolver,
    )
    router = Router({"groq": provider}, [ModelRef.parse("groq/undecided-model")])
    run(chat_ok(router, message))
    wire = transport.calls[0].payload["messages"][1]
    assert isinstance(wire["content"], str)
    assert wire["content"] == "what is in this image?"
    assert "blocks" not in wire


def test_provider_error_falls_back_to_next_vision_model(tmp_path: Path) -> None:
    from eurus.providers.transport import HttpError

    store = make_store(tmp_path)
    asset_id = save_image(store, PNG_BYTES)
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    message = Message.user("describe", (ImageBlock(asset_id=asset_id, mime_type="image/png"),))
    broken = FakeTransport(json_responses=[HttpError(500, "down")])
    healthy = FakeTransport(json_responses=[ANTHROPIC_OK])
    openai_provider = OpenAICompatProvider(
        BUILTIN_PROVIDERS["openai"],
        "k",
        broken,
        RetryPolicy(max_retries=0),
        RecordingSleep(),
        asset_resolver=resolver,
    )
    anthropic_provider = AnthropicProvider(
        BUILTIN_PROVIDERS["anthropic"],
        "k",
        healthy,
        RetryPolicy(max_retries=0),
        RecordingSleep(),
        asset_resolver=resolver,
    )
    router = Router(
        {"openai": openai_provider, "anthropic": anthropic_provider},
        [ModelRef.parse("openai/gpt-4o"), ModelRef.parse("anthropic/claude-sonnet-4")],
    )
    response = run(chat_ok(router, message))
    assert response.message.content == "a diagram"
    assert broken.calls[0].payload["messages"][1]["content"][1]["type"] == "image_url"
    assert healthy.calls[0].payload["messages"][0]["content"][1]["type"] == "image"


def test_streaming_vision_response(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    asset_id = save_image(store, PNG_BYTES)
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    message = Message.user("describe", (ImageBlock(asset_id=asset_id, mime_type="image/png"),))
    transport = FakeTransport(streams=[openai_stream("a red square")])
    router = openai_vision_router(transport, resolver)

    async def collect_stream():
        texts = []
        async for event in router.stream((Message.system("sys"), message)):
            if event.kind == "text":
                texts.append(event.text)
        return texts

    texts = run(collect_stream())
    assert "".join(texts) == "a red square"
    content = transport.calls[0].payload["messages"][1]["content"]
    assert any(part["type"] == "image_url" for part in content)


def test_agent_conversation_with_text_and_image(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    asset_id = save_image(store, PNG_BYTES)
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    transport = FakeTransport(streams=[openai_stream("it is a one pixel png")])
    router = openai_vision_router(transport, resolver)
    context = ToolContext(workspace=tmp_path, max_output=1000, timeout=5.0)
    agent = Agent(
        router,
        ToolRegistry(),
        context,
        system_prompt="sys",
        max_steps=3,
        stream=True,
    )
    block = ImageBlock(asset_id=asset_id, mime_type="image/png")

    async def go():
        return [event async for event in agent.run("what is in this image?", blocks=(block,))]

    events = run(go())
    analyzing = [event for event in events if event.kind == "media_input_analyzing"]
    assert len(analyzing) == 1
    assert analyzing[0].asset_id == asset_id
    assert analyzing[0].media_kind == "image"
    content = transport.calls[0].payload["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "what is in this image?"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert events[-1].kind == "done"
    user_message = next(m for m in agent.history if m.role == "user")
    assert user_message.blocks == (block,)


def test_agent_keeps_image_context_across_turns(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    asset_id = save_image(store, PNG_BYTES)
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    transport = FakeTransport(streams=[openai_stream("first"), openai_stream("second")])
    router = openai_vision_router(transport, resolver)
    context = ToolContext(workspace=tmp_path, max_output=1000, timeout=5.0)
    agent = Agent(
        router,
        ToolRegistry(),
        context,
        system_prompt="sys",
        max_steps=3,
        stream=True,
    )
    block = ImageBlock(asset_id=asset_id, mime_type="image/png")

    async def go():
        async for _ in agent.run("describe this", blocks=(block,)):
            pass
        async for _ in agent.run("and its size?"):
            pass

    run(go())
    assert len(transport.calls) == 2
    second_content = transport.calls[1].payload["messages"][1]["content"]
    assert any(part["type"] == "image_url" for part in second_content)


def test_required_modalities_for_messages() -> None:
    image = ImageBlock(asset_id="a" * 32, mime_type="image/png")
    document = DocumentBlock(asset_id="b" * 32, mime_type="application/pdf")
    plain = Message.user("hi")
    assert required_modalities_for_messages([plain]) == ("text",)
    assert required_modalities_for_messages([Message.user("hi", (image,))]) == ("text", "image")
    assert required_modalities_for_messages([Message.user("hi", (image, document))]) == (
        "text",
        "image",
        "document",
    )
    assistant = Message.assistant("done", blocks=(image,))
    assert required_modalities_for_messages([assistant]) == ("text", "image")


def test_strip_message_blocks() -> None:
    image = ImageBlock(asset_id="a" * 32, mime_type="image/png")
    user = Message.user("look", (image,))
    assistant = Message.assistant("done")
    stripped = strip_message_blocks((user, assistant))
    assert stripped[0].blocks == ()
    assert stripped[0].content == "look"
    assert stripped[1] is assistant


def test_collect_block_assets_prefers_recent_messages() -> None:
    shared = "a" * 32
    old = Message.user("old", (ImageBlock(asset_id=shared, mime_type="image/png"),))
    recent = Message.user("new", (ImageBlock(asset_id=shared, mime_type="image/png"),))
    collected = collect_block_assets((old, recent))
    assert list(collected) == [shared]


def test_openai_document_block_stays_on_text_bridge(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    pdf_id = store.save("s1", "doc.pdf", PDF_BYTES).id
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    message = Message.user(
        "read this", (DocumentBlock(asset_id=pdf_id, mime_type="application/pdf"),)
    )
    transport = FakeTransport(json_responses=[OPENAI_OK])
    router = openai_vision_router(transport, resolver)
    run(chat_ok(router, message))
    wire = transport.calls[0].payload["messages"][1]
    assert isinstance(wire["content"], str)


def test_anthropic_inline_caps_guard_large_documents() -> None:
    resolver = StaticResolver(
        {"c" * 32: ResolvedAsset("c" * 32, "application/pdf", PDF_BYTES, "doc.pdf")}
    )
    message = Message.user("read", (DocumentBlock(asset_id="c" * 32, mime_type="application/pdf"),))
    tight = dict(ANTHROPIC_INLINE_BYTES)
    tight["application/"] = 4
    assets = run(
        resolve_request_assets(
            (message,),
            resolver,
            allowed_mimes=frozenset({"application/pdf"}),
            inline_bytes=tight,
        )
    )
    assert assets == {}
