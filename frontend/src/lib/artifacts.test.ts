import { describe, expect, it } from "vitest";
import { artifactFileName, buildArtifact, previewDocument } from "./artifacts";

describe("artifacts", () => {
  it("treats a short code block as inline", () => {
    expect(buildArtifact("python", "print(1)")).toBeNull();
  });

  it("promotes a long code block", () => {
    const code = Array.from({ length: 24 }, (_, index) => `x${index} = ${index}`).join("\n");
    expect(buildArtifact("python", code)?.kind).toBe("code");
  });

  it("promotes html while streaming and reads the title", () => {
    const artifact = buildArtifact("html", "<title>Demo</title><p>hi</p>", true);
    expect(artifact?.kind).toBe("html");
    expect(artifact?.title).toBe("Demo");
  });

  it("never promotes mermaid", () => {
    expect(buildArtifact("mermaid", "graph TD\nA-->B")).toBeNull();
  });

  it("injects a restrictive csp into previews", () => {
    const artifact = buildArtifact("html", "<html><head></head><body></body></html>", true);
    expect(artifact && previewDocument(artifact)).toContain("connect-src 'none'");
  });

  it("builds a safe file name", () => {
    const artifact = buildArtifact("html", "<title>My Page!</title><p>x</p>", true);
    expect(artifact && artifactFileName(artifact)).toBe("my-page.html");
  });
});
