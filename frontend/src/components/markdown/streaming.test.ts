import { describe, expect, it } from "vitest";
import {
  blockKey,
  detectUnterminatedFence,
  extractTrailingCode,
  extForLang,
  normalizeLang,
} from "./streaming";

describe("detectUnterminatedFence", () => {
  it("detects a trailing open fence", () => {
    expect(detectUnterminatedFence("text\n```python\nprint('hi')")).toBe(true);
  });

  it("accepts a closed fence", () => {
    expect(detectUnterminatedFence("text\n```python\nprint('hi')\n```")).toBe(false);
  });

  it("handles multiple closed fences", () => {
    expect(detectUnterminatedFence("```js\na\n```\npara\n```ts\nb\n```")).toBe(false);
  });

  it("supports tilde fences", () => {
    expect(detectUnterminatedFence("~~~\nstill open")).toBe(true);
    expect(detectUnterminatedFence("~~~\nclosed\n~~~")).toBe(false);
  });

  it("does not confuse inline backticks with fences", () => {
    expect(detectUnterminatedFence("some `code` and ```inline``` text")).toBe(false);
  });
});

describe("extractTrailingCode", () => {
  it("returns the trailing open code content", () => {
    expect(extractTrailingCode("intro\n```ts\nconst x = 1;")).toBe("const x = 1;");
  });

  it("returns null when all fences are closed", () => {
    expect(extractTrailingCode("```ts\nconst x = 1;\n```\ndone")).toBeNull();
  });

  it("keeps multi-line trailing content", () => {
    expect(extractTrailingCode("```sql\nSELECT 1;\nSELECT 2;")).toBe("SELECT 1;\nSELECT 2;");
  });
});

describe("normalizeLang / extForLang", () => {
  it("maps common aliases", () => {
    expect(normalizeLang("js")).toBe("javascript");
    expect(normalizeLang("TS")).toBe("typescript");
    expect(normalizeLang("Python")).toBe("python");
    expect(normalizeLang("")).toBe("text");
  });

  it("provides download extensions", () => {
    expect(extForLang("python")).toBe("py");
    expect(extForLang("bash")).toBe("sh");
    expect(extForLang("text")).toBe("txt");
  });
});

describe("blockKey", () => {
  it("is stable for identical content and differs for edits", () => {
    expect(blockKey("b", "same")).toBe(blockKey("b", "same"));
    expect(blockKey("b", "same")).not.toBe(blockKey("b", "same2"));
  });
});
