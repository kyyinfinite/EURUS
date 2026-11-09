import pytest

from eurus.config import ConfigError, load_config
from eurus.content import (
    ArtifactBlock,
    AudioBlock,
    DocumentBlock,
    ImageBlock,
    TextBlock,
    VideoBlock,
    VideoFrameBlock,
)
from eurus.models import ModelRef
from eurus.providers.capabilities import (
    capabilities_for_protocol,
    capability_view,
    model_capability_view,
)
from eurus.providers.factory import _registry_from_config
from eurus.providers.model_capabilities import (
    CAPABILITY_KEYS,
    MODEL_DECLARATIONS,
    PROTOCOL_DEFAULTS,
    CapabilityRegistry,
    ModelCapabilities,
    RoutingDecision,
    UnsupportedModalityError,
    capable_chain,
    default_registry,
    missing_modalities,
    plan_routing,
    required_modalities,
    reset_default_registry,
)
from eurus.providers.router import Router
from eurus.providers.testing import openai_stream, scripted_router

ASSET_A = "a" * 32
ASSET_B = "b" * 32


def test_capability_keys_cover_required_dimensions():
    assert set(CAPABILITY_KEYS) == {
        "text_input",
        "image_input",
        "audio_input",
        "video_input",
        "document_input",
        "text_output",
        "audio_output",
        "image_generation",
        "video_generation",
        "tool_calling",
        "streaming",
        "realtime_audio",
    }


def test_default_capabilities_are_text_only():
    caps = ModelCapabilities()
    assert caps.text_input and caps.text_output and caps.tool_calling and caps.streaming
    assert not caps.image_input
    assert not caps.audio_input
    assert not caps.video_input
    assert not caps.document_input
    assert not caps.audio_output
    assert not caps.image_generation
    assert not caps.video_generation
    assert not caps.realtime_audio
    assert not caps.multimodal


def test_capability_view_shape_is_backward_compatible():
    view = capability_view("openai")
    assert set(view["capabilities"]) == {
        "textInput",
        "imageInput",
        "audioInput",
        "videoInput",
        "fileInput",
        "textOutput",
        "audioOutput",
        "imageOutput",
        "videoOutput",
        "toolCalling",
        "streaming",
    }
    assert view["multimodal"] is False
    assert capabilities_for_protocol("gemini")["imageInput"] is False
    assert capabilities_for_protocol("anthropic")["imageInput"] is False
    assert capabilities_for_protocol("totally-unknown")["textInput"] is True


def test_model_capabilities_roundtrip_view():
    caps = ModelCapabilities(image_input=True, document_input=True)
    restored = ModelCapabilities.from_view(caps.to_view())
    assert restored == caps
    assert ModelCapabilities.from_view({"imageInput": True, "toolCalling": True}) == (
        ModelCapabilities(tool_calling=True, image_input=True)
    )


def test_from_view_rejects_unknown_and_non_boolean():
    with pytest.raises(ValueError, match="unknown capability"):
        ModelCapabilities.from_view({"smell_input": True})
    with pytest.raises(ValueError, match="must be a boolean"):
        ModelCapabilities.from_view({"imageInput": "yes"})
    with pytest.raises(ValueError, match="mapping"):
        ModelCapabilities.from_view(["imageInput"])


@pytest.mark.parametrize(
    ("provider", "model", "field"),
    [
        ("openai", "gpt-4o", "image_input"),
        ("openai", "gpt-4o-mini", "image_input"),
        ("openai", "gpt-4.1", "document_input"),
        ("openai", "gpt-4o-audio-preview", "audio_output"),
        ("openai", "gpt-image-1", "image_generation"),
        ("anthropic", "claude-3-5-sonnet-latest", "image_input"),
        ("anthropic", "claude-sonnet-4-20250514", "document_input"),
        ("gemini", "gemini-2.5-flash", "audio_input"),
        ("gemini", "gemini-1.5-pro-002", "video_input"),
        ("gemini", "veo-3.0-generate-preview", "video_generation"),
        ("groq", "whisper-large-v3-turbo", "audio_input"),
        ("xai", "grok-4", "image_input"),
        ("openrouter", "openai/gpt-4o-mini", "image_input"),
        ("openrouter", "google/gemini-2.0-flash-001", "video_input"),
        ("ollama", "llama3.2-vision", "image_input"),
        ("mistral", "pixtral-large-latest", "image_input"),
    ],
)
def test_declared_models_expose_expected_capabilities(provider, model, field):
    registry = CapabilityRegistry()
    assert getattr(registry.lookup(provider, model), field) is True


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-4"),
        ("openai", "gpt-3.5-turbo"),
        ("openai", "o3-mini"),
        ("anthropic", "claude-2.1"),
        ("deepseek", "deepseek-chat"),
        ("groq", "llama-3.3-70b-versatile"),
        ("together", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
        ("unknown-provider", "mystery-model"),
    ],
)
def test_undeclared_models_stay_text_only(provider, model):
    registry = CapabilityRegistry()
    caps = registry.lookup(provider, model)
    assert caps == ModelCapabilities()


def test_o1_mini_is_text_only_without_tools():
    registry = CapabilityRegistry()
    caps = registry.lookup("openai", "o1-mini")
    assert caps.text_input and caps.text_output and caps.streaming
    assert not caps.image_input and not caps.tool_calling


def test_family_matching_requires_segment_boundary():
    registry = CapabilityRegistry()
    assert registry.lookup("openai", "gpt-4o-2024-11-20").image_input is True
    assert registry.lookup("openai", "gpt-4.1-mini-2025-04-14").image_input is True
    assert registry.lookup("openai", "gpt-42").image_input is False
    assert registry.lookup("openai", "gpt-4x-custom").image_input is False


def test_longest_family_prefix_wins():
    registry = CapabilityRegistry()
    assert registry.lookup("openai", "gpt-4-turbo-2024-04-09").image_input is True
    assert registry.lookup("openai", "gpt-4-32k").image_input is False


def test_provider_level_declaration_overrides_defaults():
    registry = CapabilityRegistry()
    registry.declare_provider("acme", ModelCapabilities(image_input=True))
    assert registry.lookup("acme", "any-model").image_input is True
    assert registry.lookup("acme", "any-model").tool_calling is True


def test_exact_model_declaration_beats_family_and_provider():
    registry = CapabilityRegistry()
    registry.declare_provider("acme", ModelCapabilities(image_input=True))
    registry.declare_model("acme", "acme-vision-xl", ModelCapabilities(audio_input=True))
    assert registry.lookup("acme", "acme-vision-xl").image_input is False
    assert registry.lookup("acme", "acme-vision-xl").audio_input is True


def test_declare_validates_inputs():
    registry = CapabilityRegistry()
    with pytest.raises(ValueError):
        registry.declare_model("", "model", ModelCapabilities())
    with pytest.raises(ValueError):
        registry.declare_model("acme", "  ", ModelCapabilities())
    with pytest.raises(ValueError):
        registry.declare_provider("", ModelCapabilities())


def test_declared_models_listing():
    registry = CapabilityRegistry()
    models = registry.declared_models("openai")
    assert "gpt-4o" in models
    assert registry.declared_models("no-such-provider") == ()


def test_registry_isolation_from_default():
    empty: dict[tuple[str, str], ModelCapabilities] = {}
    registry = CapabilityRegistry(model_declarations=empty)
    assert registry.lookup("openai", "gpt-4o") == ModelCapabilities()
    assert default_registry().lookup("openai", "gpt-4o").image_input is True


def test_lookup_ref_uses_provider_and_model():
    registry = CapabilityRegistry()
    ref = ModelRef.parse("gemini/gemini-2.5-pro")
    assert registry.lookup_ref(ref).video_input is True


def test_required_modalities_from_blocks():
    blocks = (
        TextBlock("hi"),
        ImageBlock(ASSET_A, "image/png"),
        AudioBlock(ASSET_B, "audio/mpeg"),
        DocumentBlock(ASSET_A, "application/pdf"),
        VideoFrameBlock(ASSET_A, ASSET_B, "image/jpeg", 1.0),
        ArtifactBlock(ASSET_A),
    )
    assert required_modalities(blocks) == ("text", "image", "audio", "document", "artifact")
    assert required_modalities(()) == ("text",)
    assert required_modalities((TextBlock("hi"),)) == ("text",)


def test_video_block_without_frames_requires_video_modality():
    blocks = (VideoBlock(ASSET_B, "video/mp4"),)
    assert required_modalities(blocks) == ("text", "video")


def test_video_block_with_frames_satisfied_by_images():
    blocks = (
        VideoBlock(ASSET_B, "video/mp4"),
        VideoFrameBlock(ASSET_A, ASSET_B, "image/jpeg", 1.0),
    )
    assert required_modalities(blocks) == ("text", "image")


def test_video_frame_without_source_video_is_image_modality():
    blocks = (VideoFrameBlock(ASSET_A, ASSET_B, "image/jpeg", 2.5),)
    assert required_modalities(blocks) == ("text", "image")


def test_missing_modalities_reports_gaps():
    caps = ModelCapabilities()
    assert missing_modalities(caps, ("text", "image")) == ("image",)
    vision = ModelCapabilities(image_input=True)
    assert missing_modalities(vision, ("text", "image")) == ()


def test_plan_routing_selects_first_capable_model():
    chain = [
        ModelRef.parse("deepseek/deepseek-chat"),
        ModelRef.parse("openai/gpt-4o"),
        ModelRef.parse("gemini/gemini-2.5-flash"),
    ]
    decision = plan_routing(chain, ("text", "image"))
    assert decision.ok
    assert decision.selected == ModelRef.parse("openai/gpt-4o")
    assert decision.required == ("text", "image")
    assert decision.rejected == ((ModelRef.parse("deepseek/deepseek-chat"), ("image",)),)


def test_plan_routing_video_input_needs_multimodal_model():
    chain = [
        ModelRef.parse("openai/gpt-4o"),
        ModelRef.parse("gemini/gemini-2.5-flash"),
    ]
    decision = plan_routing(chain, ("text", "video"))
    assert decision.selected == ModelRef.parse("gemini/gemini-2.5-flash")


def test_plan_routing_fails_with_structured_error():
    chain = [
        ModelRef.parse("deepseek/deepseek-chat"),
        ModelRef.parse("groq/llama-3.3-70b-versatile"),
    ]
    decision = plan_routing(chain, ("text", "image", "audio"))
    assert not decision.ok
    error = decision.error()
    assert isinstance(error, UnsupportedModalityError)
    assert error.required == ("text", "image", "audio")
    assert [ref.model for ref, _ in error.candidates] == [
        "deepseek-chat",
        "llama-3.3-70b-versatile",
    ]
    assert error.candidates[0][1] == ("image", "audio")
    with pytest.raises(ValueError):
        RoutingDecision(ModelRef.parse("openai/gpt-4o"), ("text",), ()).error()


def test_capable_chain_filters_in_order():
    chain = [
        ModelRef.parse("deepseek/deepseek-chat"),
        ModelRef.parse("openai/gpt-4o"),
        ModelRef.parse("gemini/gemini-2.5-flash"),
    ]
    capable = capable_chain(chain, ("text", "image"))
    assert capable == (
        ModelRef.parse("openai/gpt-4o"),
        ModelRef.parse("gemini/gemini-2.5-flash"),
    )
    assert capable_chain(chain, ("text",)) == tuple(chain)


def test_routing_uses_custom_registry():
    registry = CapabilityRegistry(model_declarations={})
    registry.declare_model("local", "vision-model", ModelCapabilities(image_input=True))
    chain = [ModelRef.parse("local/text-model"), ModelRef.parse("local/vision-model")]
    decision = plan_routing(chain, ("text", "image"), registry=registry)
    assert decision.selected == ModelRef.parse("local/vision-model")


def test_routing_respects_protocol_defaults():
    registry = CapabilityRegistry(model_declarations={})
    chain = [ModelRef.parse("custom/mystery")]
    decision = plan_routing(chain, ("text",), registry=registry, protocols={"custom": "openai"})
    assert decision.selected == ModelRef.parse("custom/mystery")
    assert decision.selected is not None


def test_router_exposes_capabilities_for_chain():
    router, _ = scripted_router(streams=[openai_stream("ok")])
    assert hasattr(router, "capability_registry")
    ref = router.chain[0]
    caps = router.capabilities_for(ref)
    assert caps.text_input is True
    assert caps.image_input is False


def test_router_accepts_custom_registry():
    router, _ = scripted_router(streams=[openai_stream("ok")])
    ref = router.chain[0]
    registry = CapabilityRegistry()
    registry.declare_model(ref.provider, ref.model, ModelCapabilities(image_input=True))
    custom_router = Router(
        router.providers,
        router.chain,
        capability_registry=registry,
    )
    assert custom_router.capabilities_for(ref).image_input is True


def test_vision_supported_with_vision_model_in_chain():
    from eurus.office.qa.visual import vision_supported

    router, _ = scripted_router(streams=[openai_stream("ok")])
    ref = router.chain[0]
    registry = CapabilityRegistry()
    registry.declare_model(ref.provider, ref.model, ModelCapabilities(image_input=True))
    vision_router = Router(router.providers, router.chain, capability_registry=registry)
    assert vision_supported(vision_router) is True
    assert vision_supported(router) is False
    assert vision_supported(None) is False


def test_provider_usage_metadata_via_registry():
    registry = CapabilityRegistry()
    caps = registry.lookup("openai", "gpt-4o")
    view = caps.to_view()
    assert view["textInput"] is True
    assert view["imageInput"] is True
    assert view["toolCalling"] is True


def test_model_capability_view_reports_multimodal():
    view = model_capability_view("openai", "gpt-4o", protocol="openai")
    assert view["multimodal"] is True
    assert view["capabilities"]["imageInput"] is True
    plain = model_capability_view("deepseek", "deepseek-chat", protocol="openai")
    assert plain["multimodal"] is False


def test_provider_availability_falls_to_next_capable_model():
    chain = [
        ModelRef.parse("openai/gpt-4o"),
        ModelRef.parse("gemini/gemini-2.5-flash"),
    ]
    registry = CapabilityRegistry()
    capable = capable_chain(chain, ("text", "image"), registry=registry)
    decision = plan_routing(capable or chain, ("text", "image"), registry=registry)
    assert decision.selected == ModelRef.parse("openai/gpt-4o")


def test_toml_capability_configuration(tmp_path):
    config_text = (
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
        'api_key_env = "DUMMY_KEY"\n\n'
        "[providers.dummy.capabilities]\nimage_input = true\ntext_input = true\n"
        "document_input = false\n"
    )
    (tmp_path / "config.toml").write_text(config_text, encoding="utf-8")
    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    spec = config.providers["dummy"]
    assert set(spec.capabilities) == {
        ("image_input", True),
        ("text_input", True),
        ("document_input", False),
    }
    registry = _registry_from_config(config)
    assert registry.lookup("dummy", "anything").image_input is True
    assert registry.lookup("dummy", "anything").tool_calling is True
    assert registry.lookup("openai", "gpt-4o").image_input is True


def test_toml_capability_configuration_accepts_camel_case(tmp_path):
    config_text = (
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n\n'
        "[providers.dummy.capabilities]\nimageInput = true\n"
    )
    (tmp_path / "config.toml").write_text(config_text, encoding="utf-8")
    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    assert ("image_input", True) in config.providers["dummy"].capabilities


def test_toml_capability_configuration_rejects_unknown_keys(tmp_path):
    config_text = (
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n\n'
        "[providers.dummy.capabilities]\nsmell_input = true\n"
    )
    (tmp_path / "config.toml").write_text(config_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown capability"):
        load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})


def test_toml_capability_configuration_rejects_non_boolean(tmp_path):
    config_text = (
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n\n'
        '[providers.dummy.capabilities]\nimage_input = "yes"\n'
    )
    (tmp_path / "config.toml").write_text(config_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a boolean"):
        load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})


def test_existing_configs_without_capabilities_section_load(tmp_path):
    config_text = (
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
        'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n'
    )
    (tmp_path / "config.toml").write_text(config_text, encoding="utf-8")
    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    assert config.providers["dummy"].capabilities == ()
    registry = _registry_from_config(config)
    assert registry is default_registry()


def test_default_registry_is_stable_across_calls():
    first = default_registry()
    assert default_registry() is first
    reset_default_registry()
    assert default_registry() is not first
    reset_default_registry(None)


def test_protocol_defaults_are_text_only():
    for protocol in ("openai", "anthropic", "gemini", "groq-sdk", "genai-sdk"):
        assert PROTOCOL_DEFAULTS[protocol] == ModelCapabilities()


def test_declarations_only_reference_real_providers():
    known = {
        "openai",
        "anthropic",
        "gemini",
        "gemini-sdk",
        "groq",
        "groq-sdk",
        "mistral",
        "xai",
        "openrouter",
        "ollama",
    }
    for provider, _ in MODEL_DECLARATIONS:
        assert provider in known
