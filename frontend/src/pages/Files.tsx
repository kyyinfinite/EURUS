/** Files & Artifacts — every uploaded file in the canonical media store,
 *  newest first, with previews, authenticated downloads and deletion.
 *
 *  Honest scope note: this lists the canonical media store (uploads made
 *  through any interface). Files the agent writes into its workspace are not
 *  part of this store — exposing them needs a workspace-artifacts endpoint
 *  (documented in docs/WEB_GATEWAY.md).
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileStack, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import type { MediaView } from "../lib/types";
import { useUI } from "../store/ui";
import { formatRelative } from "../lib/format";
import { Button, EmptyState, ErrorState, LoadingState, Modal, PageHeader, SearchInput } from "../components/ui";
import { FileCard } from "../components/files/FileCard";
import { ImageViewer } from "../components/media/MediaBlocks";
import type { AttachmentRef } from "../lib/types";

type KindFilter = "all" | "image" | "document" | "audio" | "video";

const FILTERS: { key: KindFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "image", label: "Images" },
  { key: "document", label: "Documents & code" },
  { key: "audio", label: "Audio" },
  { key: "video", label: "Video" },
];

export default function Files() {
  const queryClient = useQueryClient();
  const pushToast = useUI((state) => state.pushToast);
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState<KindFilter>("all");
  const [viewer, setViewer] = useState<AttachmentRef | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<MediaView | null>(null);

  const mediaQuery = useQuery({
    queryKey: ["media"],
    queryFn: () => api.get<MediaView[]>("/api/media?limit=500"),
    refetchInterval: 15_000,
  });

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

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Files"
        icon={<FileStack size={20} />}
        subtitle={`${media.length} file${media.length === 1 ? "" : "s"} in the canonical media store`}
        actions={
          <Button
            onClick={() => void queryClient.invalidateQueries({ queryKey: ["media"] })}
            disabled={mediaQuery.isFetching}
          >
            <RefreshCw size={14} className={mediaQuery.isFetching ? "animate-spin" : ""} /> Refresh
          </Button>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-28 sm:px-6 md:pb-12 lg:px-8">
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
