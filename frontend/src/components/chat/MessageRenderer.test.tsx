import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MessageRenderer, groupContentBlocks } from "./MessageRenderer";
import type { ContentBlockView, MessageView } from "../../lib/types";

vi.mock("../../lib/api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
  fetchBlobUrl: vi.fn(),
}));

import { api, fetchBlobUrl } from "../../lib/api";

const getMock = vi.mocked(api.get);
const blobMock = vi.mocked(fetchBlobUrl);

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
});

function renderMessage(message: MessageView) {
  return render(
    <QueryClientProvider client={queryClient}>
      <MessageRenderer message={message} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  queryClient.clear();
});

function viewMessage(message: Partial<MessageView>): MessageView {
  return {
    sequence: 1,
    role: "user",
    content: "",
    tool_calls: [],
    tool_call_id: null,
    name: null,
    ...message,
  };
}

const ASSET = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6";
const AUDIO_ASSET = ("d".repeat(32));
const GENERATED_ASSET = ("e".repeat(32));

describe("groupContentBlocks", () => {
  it("groups video frames under their source video block", () => {
    const blocks: ContentBlockView[] = [
      { type: "text", data: { type: "text", text: "watch this" } },
      { type: "video", data: { type: "video", asset_id: ASSET, mime_type: "video/mp4" } },
      { type: "video_frame", data: { type: "video_frame", asset_id: "b".repeat(32), source_asset_id: ASSET, mime_type: "image/jpeg", timestamp_seconds: 2.5 } },
      { type: "video_frame", data: { type: "video_frame", asset_id: "c".repeat(32), source_asset_id: ASSET, mime_type: "image/jpeg", timestamp_seconds: 5 } },
    ];
    const grouped = groupContentBlocks(blocks);
    expect(grouped).toHaveLength(2);
    expect(grouped[0]?.kind).toBe("text");
    expect(grouped[1]?.kind).toBe("video");
    expect(grouped[1]?.frames).toHaveLength(2);
    expect(grouped[1]?.frames?.[0]?.timestamp_seconds).toBe(2.5);
  });

  it("renders orphan frames as images when the source video is missing", () => {
    const blocks: ContentBlockView[] = [
      { type: "video_frame", data: { type: "video_frame", asset_id: ASSET, source_asset_id: "b".repeat(32), mime_type: "image/jpeg", timestamp_seconds: 1 } },
    ];
    const grouped = groupContentBlocks(blocks);
    expect(grouped[0]?.kind).toBe("image");
  });
});

describe("MessageRenderer block rendering", () => {
  it("renders native image blocks instead of marker text", async () => {
    blobMock.mockResolvedValue("blob:image");
    const message = viewMessage({
      role: "user",
      content: "what is this?",
      blocks: [
        { type: "text", data: { type: "text", text: "what is this?" } },
        { type: "image", data: { type: "image", asset_id: ASSET, mime_type: "image/png", alt_text: "photo.png" } },
      ],
    });
    renderMessage(message);
    expect(await screen.findByRole("button", { name: "View image photo.png" })).toBeTruthy();
  });

  it("renders an audio block with a transcript disclosure", async () => {
    blobMock.mockResolvedValue("blob:audio");
    getMock.mockResolvedValue({
      id: AUDIO_ASSET,
      session_id: "s1",
      filename: "note.mp3",
      mime: "audio/mpeg",
      kind: "audio",
      size: 1024,
      sha256: "x",
      created_at: "",
      url: `/api/media/${AUDIO_ASSET}`,
    });
    const message = viewMessage({
      role: "user",
      content: "listen",
      blocks: [
        { type: "audio", data: { type: "audio", asset_id: AUDIO_ASSET, mime_type: "audio/mpeg", transcript: "spoken words" } },
      ],
    });
    renderMessage(message);
    expect(await screen.findByRole("button", { name: "Play audio" })).toBeTruthy();
    expect(screen.getByText("Transcript")).toBeTruthy();
  });

  it("renders a video block with keyframe navigation", async () => {
    blobMock.mockResolvedValue("blob:video");
    const frameId = "b".repeat(32);
    const message = viewMessage({
      role: "user",
      content: "summarize",
      blocks: [
        { type: "video", data: { type: "video", asset_id: ASSET, mime_type: "video/mp4", duration_seconds: 12.5, transcript: "hello" } },
        { type: "video_frame", data: { type: "video_frame", asset_id: frameId, source_asset_id: ASSET, mime_type: "image/jpeg", timestamp_seconds: 4 } },
      ],
    });
    renderMessage(message);
    const jump = await screen.findByRole("button", { name: "Jump to 0:04" });
    expect(jump).toBeTruthy();
    expect(screen.getByText("Video transcript")).toBeTruthy();
  });

  it("renders artifact blocks as downloadable artifact cards", async () => {
    getMock.mockResolvedValue({
      artifact_id: "art-1",
      owner_id: "eurus",
      mission_id: "standalone",
      task_id: null,
      assignment_id: null,
      artifact_type: "image",
      media_type: "image/png",
      content_hash: "f".repeat(64),
      size_bytes: 2048,
      version: 1,
      metadata: {},
      allowed_recipients: [],
      created_at: "2026-11-01T00:00:00Z",
      name: "generated-image-1.png",
      source_tool: "generate_image",
      run_id: "run-1",
      expires_at: null,
    });
    const message = viewMessage({
      role: "assistant",
      content: "Here is your image.",
      blocks: [{ type: "artifact", data: { type: "artifact", artifact_id: "art-1", artifact_type: "image", name: "generated-image-1.png" } }],
    });
    renderMessage(message);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Download generated-image-1.png" })).toBeTruthy(),
    );
    expect(screen.getByText("Generated image")).toBeTruthy();
  });

  it("falls back to legacy attachment markers when blocks are absent", async () => {
    blobMock.mockResolvedValue("blob:image");
    const content =
      "check this\n[attach id=abc123def456abc123def456abc12345 kind=image name=photo%2Epng mime=image%2Fpng size=2048]";
    const message = viewMessage({ role: "user", content });
    renderMessage(message);
    expect(await screen.findByRole("button", { name: "View image photo.png" })).toBeTruthy();
  });

  it("resolves generated media markers in assistant answers", async () => {
    getMock.mockResolvedValue({
      id: GENERATED_ASSET,
      session_id: "s1",
      filename: "generated-image-1.png",
      mime: "image/png",
      kind: "image",
      size: 4096,
      sha256: "x",
      created_at: "",
      url: `/api/media/${GENERATED_ASSET}`,
    });
    blobMock.mockResolvedValue("blob:generated");
    const message = viewMessage({
      role: "assistant",
      content: `Here is your image:\n[image:${GENERATED_ASSET}]`,
    });
    renderMessage(message);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "View image generated-image-1.png" })).toBeTruthy(),
    );
    expect(screen.queryByText(`[image:${GENERATED_ASSET}]`)).toBeNull();
  });

  it("renders assistant markdown text blocks", () => {
    const message = viewMessage({
      role: "assistant",
      content: "done",
      blocks: [{ type: "text", data: { type: "text", text: "Here is **bold** text." } }],
    });
    renderMessage(message);
    expect(screen.getByText("bold")).toBeTruthy();
  });
});
