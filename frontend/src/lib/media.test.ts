import { describe, expect, it } from "vitest";
import { parseMessageContent, kindFromFile } from "./media";


describe("parseMessageContent", () => {
  it("returns a single text segment for plain messages", () => {
    const segments = parseMessageContent("hello world");
    expect(segments).toEqual([{ type: "text", text: "hello world" }]);
  });

  it("parses an attachment marker into an attachment segment", () => {
    const content =
      'what is in this image?\n\n[attach id=abc123def456abc123def456abc12345 kind=image name=photo%2Epng mime=image%2Fpng size=2048]\nAttachment: photo.png (image/png, 2048 bytes)';
    const segments = parseMessageContent(content);
    expect(segments).toHaveLength(3);
    expect(segments[0]).toEqual({ type: "text", text: "what is in this image?\n\n" });
    expect(segments[1]?.type).toBe("attachment");
    expect(segments[1]?.attachment).toEqual({
      id: "abc123def456abc123def456abc12345",
      kind: "image",
      name: "photo.png",
      mime: "image/png",
      size: 2048,
    });
    // the backend annotation line after the marker stays as text (hidden by the user renderer)
    expect(segments[2]?.type).toBe("text");
    expect(segments[2]?.text).toContain("Attachment: photo.png");
  });

  it("handles multiple attachments and interleaved text", () => {
    const a = "[attach id=00000000000000000000000000000001 kind=document name=a%2Etxt mime=text%2Fplain size=10]";
    const b = "[attach id=00000000000000000000000000000002 kind=audio name=b%2Emp3 mime=audio%2Fmpeg size=99]";
    const segments = parseMessageContent(`${a}\nmiddle text\n${b}`);
    expect(segments.map((segment) => segment.type)).toEqual([
      "attachment",
      "text",
      "attachment",
    ]);
    expect(segments[1]?.text).toContain("middle text");
    expect(segments[2]?.attachment?.kind).toBe("audio");
  });

  it("returns empty for empty content", () => {
    expect(parseMessageContent("")).toEqual([]);
  });
});

describe("kindFromFile", () => {
  it("detects images by mime", () => {
    expect(kindFromFile(new File([], "x.png", { type: "image/png" }))).toBe("image");
  });

  it("detects audio by mime", () => {
    expect(kindFromFile(new File([], "x.mp3", { type: "audio/mpeg" }))).toBe("audio");
  });

  it("falls back to the extension when the mime is empty", () => {
    expect(kindFromFile(new File([], "notes.pdf", { type: "" }))).toBe("document");
    expect(kindFromFile(new File([], "clip.webm", { type: "" }))).toBe("video");
  });

  it("returns null for unsupported types", () => {
    expect(kindFromFile(new File([], "app.exe", { type: "application/octet-stream" }))).toBeNull();
  });
});

describe("kindFromFile — code and data sources", () => {
  it("detects python and typescript sources by extension", () => {
    expect(kindFromFile(new File([], "script.py", { type: "text/x-python" }))).toBe("document");
    expect(kindFromFile(new File([], "app.ts", { type: "" }))).toBe("document");
    expect(kindFromFile(new File([], "page.html", { type: "" }))).toBe("document");
  });

  it("detects xml documents", () => {
    expect(kindFromFile(new File([], "data.xml", { type: "text/xml" }))).toBe("document");
  });

  it("rejects mov video (the canonical store accepts mp4/webm only)", () => {
    expect(kindFromFile(new File([], "clip.mov", { type: "video/quicktime" }))).toBeNull();
  });

  it("keeps webm classified as video by extension", () => {
    expect(kindFromFile(new File([], "clip.webm", { type: "" }))).toBe("video");
  });
});
