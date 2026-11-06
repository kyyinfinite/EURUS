
import { useMemo, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileStack, RefreshCw } from "lucide-react";
import { api, fetchBlobUrl } from "../lib/api";
import type { ArtifactPage, ArtifactView, MediaView } from "../lib/types";
import { useUI } from "../store/ui";
import { formatRelative } from "../lib/format";
import { Button, EmptyState, ErrorState, LoadingState, Modal, PageHeader, SearchInput } from "../components/ui";
import { FileCard } from "../components/files/FileCard";
import { ImageViewer } from "../components/media/MediaBlocks";
import type { AttachmentRef } from "../lib/types";

type KindFilter = "all" | "image" | "document" | "audio" | "video";
type FilesTab = "uploads" | "artifacts";

const FILTERS: { key: KindFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "image", label: "Images" },
  { key: "document", label: "Documents & code" },
  { key: "audio", label: "Audio" },
  { key: "video", label: "Video" },
];

const ARTIFACT_PAGE_SIZE = 50;

export default function Files() {
  const queryClient = useQueryClient();
  const pushToast = useUI((state) => state.pushToast);
  const [tab, setTab] = useState<FilesTab>("uploads");
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState<KindFilter>("all");
  const [viewer, setViewer] = useState<AttachmentRef | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<MediaView | null>(null);

  const mediaQuery = useQuery({
    queryKey: ["media"],
    queryFn: () => api.get<MediaView[]>("/api/media?limit=500"),
    refetchInterval: 15_000,
  });

  const artifactsQuery = useInfiniteQuery({
    queryKey: ["artifacts"],
    queryFn: ({ pageParam }) =>
      api.get<ArtifactPage>(
        pageParam === null
          ? `/api/artifacts?limit=${ARTIFACT_PAGE_SIZE}`
          : `/api/artifacts?limit=${ARTIFACT_PAGE_SIZE}&cursor=${encodeURIComponent(pageParam)}`,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    enabled: tab === "artifacts",
  });

  const downloadArtifact = async (artifact: ArtifactView) => {
    try {
      const url = await fetchBlobUrl(`/api/artifacts/${artifact.artifact_id}/content`);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = artifact.name || `artifact-${artifact.artifact_id.slice(0, 8)}`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    } catch (error) {
      pushToast({
        tone: "danger",
        title: "Download failed",
        detail: error instanceof Error ? error.message : undefined,
      });
    }
  };

  const remove = useMutation({
    mutationFn: (mediaId: string) => api.delete(`/api/media/${mediaId}`),
    onSuccess: () => {
      setConfirmDelete(null);
      void queryClient.invalidateQueries({ queryKey: ["media"] });
      pushToast({ tone: "info", title: "File deleted" });
    },
    onError: (error) =>
      pushToast({
        tone: "danger",
        title: "Delete failed",
        detail: error instanceof Error ? error.message : undefined,
      }),
  });

  const media = mediaQuery.data ?? [];
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return media.filter((item) => {
      if (kind !== "all" && item.kind !== kind) return false;
      if (!needle) return true;
      return (
        item.filename.toLowerCase().includes(needle) ||
        item.mime.toLowerCase().includes(needle) ||
        item.session_id.toLowerCase().includes(needle)
      );
    });
  }, [media, search, kind]);

  const artifacts = artifactsQuery.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Files"
        icon={<FileStack size={20} />}
        subtitle={
          tab === "uploads"
            ? `${media.length} file${media.length === 1 ? "" : "s"} in the canonical media store`
            : `${artifactsQuery.data?.pages[0]?.total ?? artifacts.length} artifact${
                (artifactsQuery.data?.pages[0]?.total ?? artifacts.length) === 1 ? "" : "s"
              } published by agent runs`
        }
        actions={
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 rounded-full border border-line bg-raised-soft p-0.5">
              {(
                [
                  { key: "uploads", label: "Uploads" },
                  { key: "artifacts", label: "Agent artifacts" },
                ] as { key: FilesTab; label: string }[]
              ).map((option) => (
                <button
                  key={option.key}
                  type="button"
                  onClick={() => setTab(option.key)}
                  className={`rounded-full px-3 py-1.5 text-xs font-semibold transition-colors ${
                    tab === option.key ? "bg-surface text-ink shadow-sm" : "text-muted hover:text-ink"
                  }`}
                >
                  {option.label}
                </button>
              ))}
            </div>
            {tab === "uploads" ? (
              <Button
                onClick={() => void queryClient.invalidateQueries({ queryKey: ["media"] })}
                disabled={mediaQuery.isFetching}
              >
                <RefreshCw size={14} className={mediaQuery.isFetching ? "animate-spin" : ""} /> Refresh
              </Button>
            ) : (
              <Button
                onClick={() => void queryClient.invalidateQueries({ queryKey: ["artifacts"] })}
                disabled={artifactsQuery.isFetching}
              >
                <RefreshCw size={14} className={artifactsQuery.isFetching ? "animate-spin" : ""} /> Refresh
              </Button>
            )}
          </div>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-28 sm:px-6 md:pb-12 lg:px-8">
        {tab === "uploads" ? (
          <>
            <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <SearchInput
                value={search}
                onChange={setSearch}
                placeholder="Search files by name, type or session…"
                className="sm:max-w-sm"
              />
              <div className="flex flex-wrap items-center gap-1.5">
                {FILTERS.map((filter) => (
                  <button
                    key={filter.key}
                    type="button"
                    onClick={() => setKind(filter.key)}
                    className={`rounded-full border px-3 py-1.5 text-[13px] font-medium transition-colors ${
                      kind === filter.key
                        ? "border-copper/40 bg-copper-soft text-copper-dark"
                        : "border-line bg-surface text-muted hover:text-ink"
                    }`}
                  >
                    {filter.label}
                  </button>
                ))}
              </div>
            </div>

            {mediaQuery.isLoading ? (
              <LoadingState label="Loading files…" />
            ) : mediaQuery.isError ? (
              <ErrorState
                detail="Media store unavailable"
                hint="The gateway could not list media. Check that the gateway is running."
              />
            ) : filtered.length === 0 ? (
              <EmptyState
                icon={<FileStack size={24} />}
                title={media.length === 0 ? "No files yet" : "Nothing matches this filter"}
                description={
                  media.length === 0
                    ? "Attach files in any conversation — images, documents, code, audio and video all land in this canonical store."
                    : "Try a different kind filter or clear the search."
                }
              />
            ) : (
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
                {filtered.map((item) => (
                  <FileCard
                    key={item.id}
                    media={item}
                    onDelete={setConfirmDelete}
                    onOpenImage={setViewer}
                  />
                ))}
              </div>
            )}
          </>
        ) : artifactsQuery.isLoading ? (
          <LoadingState label="Loading artifacts…" />
        ) : artifactsQuery.isError ? (
          <ErrorState
            detail="Agent artifacts unavailable"
            hint="The gateway could not list artifacts. Check that the gateway is running."
          />
        ) : artifacts.length === 0 ? (
          <EmptyState
            icon={<FileStack size={24} />}
            title="No artifacts yet"
            description="Specialists publish documents, reports and renders here as they execute missions and runs."
          />
        ) : (
          <div className="space-y-3">
            {artifacts.map((artifact) => (
              <ArtifactRow key={artifact.artifact_id} artifact={artifact} onDownload={downloadArtifact} />
            ))}
            {artifactsQuery.hasNextPage ? (
              <div className="flex justify-center pt-2">
                <Button
                  variant="outline"
                  onClick={() => void artifactsQuery.fetchNextPage()}
                  disabled={artifactsQuery.isFetchingNextPage}
                >
                  Load more
                </Button>
              </div>
            ) : null}
          </div>
        )}
      </div>

      {viewer && <ImageViewer attachment={viewer} onClose={() => setViewer(null)} />}

      <Modal
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        title="Delete file?"
        footer={
          <div className="flex justify-end gap-2">
            <Button size="sm" onClick={() => setConfirmDelete(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              size="sm"
              onClick={() => confirmDelete && remove.mutate(confirmDelete.id)}
              disabled={remove.isPending}
            >
              Delete
            </Button>
          </div>
        }
      >
        <p className="text-sm leading-relaxed text-muted">
          <span className="font-semibold text-ink">{confirmDelete?.filename}</span> (
          {formatRelative(confirmDelete?.created_at ?? null)}) will be removed from the media store.
          Messages that reference it keep their text, but the preview will stop resolving. This
          cannot be undone.
        </p>
      </Modal>
    </div>
  );
}

function ArtifactRow({
  artifact,
  onDownload,
}: {
  artifact: ArtifactView;
  onDownload: (artifact: ArtifactView) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl border border-line bg-surface px-4 py-3 shadow-card">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-sm font-semibold text-ink">{artifact.name || artifact.artifact_id}</span>
          <span className="rounded-full border border-line bg-canvas px-2 py-0.5 text-2xs text-muted">
            {artifact.artifact_type}
          </span>
          <span className="text-2xs text-faint">v{artifact.version}</span>
        </div>
        <p className="mt-0.5 truncate font-mono text-2xs text-faint">
          via {artifact.source_tool || "unknown"} · mission {artifact.mission_id.slice(0, 12)} ·{" "}
          {formatBytes(artifact.size_bytes)} · sha256:{artifact.content_hash.slice(0, 12)}
        </p>
      </div>
      <Button variant="outline" size="sm" onClick={() => onDownload(artifact)}>
        <Download size={13} /> Download
      </Button>
    </div>
  );
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
