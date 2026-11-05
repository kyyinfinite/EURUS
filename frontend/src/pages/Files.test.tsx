import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Files from "./Files";
import type { ArtifactPage, ArtifactView, MediaView } from "../lib/types";

vi.mock("../lib/api", () => ({
  api: {
    get: vi.fn(),
    delete: vi.fn(),
  },
  fetchBlobUrl: vi.fn(),
}));

import { api, fetchBlobUrl } from "../lib/api";

const getMock = vi.mocked(api.get);
const downloadMock = vi.mocked(fetchBlobUrl);

const media: MediaView[] = [
  {
    id: "media-1",
    session_id: "s-1",
    filename: "notes.txt",
    mime: "text/plain",
    kind: "document",
    size: 12,
    sha256: "abc",
    created_at: "2026-11-03T00:00:00Z",
    url: "/api/media/media-1",
  },
];

const artifacts: ArtifactView[] = [
  {
    artifact_id: "art-1",
    owner_id: "eurus",
    mission_id: "m-12345678",
    task_id: null,
    assignment_id: null,
    artifact_type: "document",
    media_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    content_hash: "f".repeat(64),
    size_bytes: 4096,
    version: 1,
    metadata: {},
    allowed_recipients: [],
    created_at: "2026-11-03T00:00:00Z",
    name: "report.docx",
    source_tool: "document_create",
    run_id: "run-1",
    expires_at: null,
  },
];

const artifactPage: ArtifactPage = {
  items: artifacts,
  next_cursor: null,
  total: 1,
};

const secondArtifact: ArtifactView = {
  ...artifacts[0],
  artifact_id: "art-2",
  created_at: "2026-11-02T00:00:00Z",
  name: "older-report.docx",
  source_tool: "document_create",
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Files />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  getMock.mockReset();
  downloadMock.mockReset();
  getMock.mockImplementation(async (path: string) => {
    if (path.startsWith("/api/media")) return media;
    if (path.startsWith("/api/artifacts")) return artifactPage;
    return [];
  }) as unknown as typeof getMock;
  downloadMock.mockResolvedValue("blob:artifact");
  Object.defineProperty(window, "URL", {
    value: {
      ...URL,
      createObjectURL: vi.fn(() => "blob:artifact"),
      revokeObjectURL: vi.fn(),
    },
    writable: true,
  });
});

afterEach(cleanup);

describe("Files page", () => {
  it("renders the uploads grid from the media store", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("notes.txt")).toBeTruthy());
    expect(getMock).toHaveBeenCalledWith("/api/media?limit=500");
  });

  it("does not fetch artifacts while the uploads tab is active", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("notes.txt")).toBeTruthy());
    const artifactCalls = getMock.mock.calls.filter(([path]) =>
      String(path).startsWith("/api/artifacts")
    );
    expect(artifactCalls).toHaveLength(0);
  });

  it("lists published artifacts with provenance on the artifacts tab", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /agent artifacts/i }));
    await waitFor(() => expect(screen.getByText("report.docx")).toBeTruthy());
    expect(screen.getByText("document")).toBeTruthy();
    expect(screen.getByText(/via document_create/)).toBeTruthy();
    expect(screen.getByText(/mission m-12345/)).toBeTruthy();
    expect(screen.getByText(/sha256:ffffffffffff/)).toBeTruthy();
  });

  it("downloads artifact content through the authenticated endpoint", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /agent artifacts/i }));
    const download = await screen.findByRole("button", { name: /download/i });
    fireEvent.click(download);
    await waitFor(() =>
      expect(downloadMock).toHaveBeenCalledWith("/api/artifacts/art-1/content")
    );
  });

  it("shows the empty state when no artifacts exist", async () => {
    getMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/media")) return media;
      return { items: [], next_cursor: null, total: 0 };
    }) as unknown as typeof getMock;
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /agent artifacts/i }));
    await waitFor(() => expect(screen.getByText("No artifacts yet")).toBeTruthy());
  });

  it("requests the first artifacts page without a cursor", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /agent artifacts/i }));
    await waitFor(() => expect(screen.getByText("report.docx")).toBeTruthy());
    expect(getMock).toHaveBeenCalledWith("/api/artifacts?limit=50");
  });

  it("loads the next page through the cursor when more artifacts exist", async () => {
    const cursor = "2026-11-02T00:00:00+00:00|art-2";
    getMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/media")) return media;
      if (path.startsWith("/api/artifacts")) {
        if (path.includes("cursor=")) {
          return { items: [secondArtifact], next_cursor: null, total: 2 };
        }
        return { items: artifacts, next_cursor: cursor, total: 2 };
      }
      return [];
    }) as unknown as typeof getMock;
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /agent artifacts/i }));
    await waitFor(() => expect(screen.getByText("report.docx")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /load more/i }));
    await waitFor(() => expect(screen.getByText("older-report.docx")).toBeTruthy());
    const cursorCalls = getMock.mock.calls.filter(([path]) =>
      String(path).includes("cursor=")
    );
    expect(cursorCalls).toHaveLength(1);
    expect(cursorCalls[0][0]).toBe(
      `/api/artifacts?limit=50&cursor=${encodeURIComponent(cursor)}`
    );
  });

  it("hides load more on the final page", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /agent artifacts/i }));
    await waitFor(() => expect(screen.getByText("report.docx")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /load more/i })).toBeNull();
  });
});
