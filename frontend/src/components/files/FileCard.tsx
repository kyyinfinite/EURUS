/** FileCard — one media row in the Files & Artifacts module. */

import { useState } from "react";
import { Trash2 } from "lucide-react";
import type { MediaView } from "../../lib/types";
import { useMediaUrl, type AttachmentRef } from "./MediaAdapters";
import { formatBytes } from "../../lib/format";
import { DocumentIcon } from "../media/MediaBlocks";
import { Badge } from "../ui";

export function FileCard({
  media,
  onDelete,
  onOpenImage,
}: {
  media: MediaView;
  onDelete: (media: MediaView) => void;
  onOpenImage: (attachment: AttachmentRef) => void;
}) {
  const attachment: AttachmentRef = {
    id: media.id,
    kind: media.kind,
    name: media.filename,
    mime: media.mime,
    size: media.size,
  };
  const [hover, setHover] = useState(false);

  return (
    <div
      className="group flex flex-col overflow-hidden rounded-xl border border-line bg-surface shadow-card transition-all hover:border-copper/40"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      {/* preview strip */}
      <div className="relative flex h-28 items-center justify-center bg-raised-soft/50">
        <FilePreview media={media} attachment={attachment} hover={hover} onOpenImage={onOpenImage} />
        <button
          type="button"
          onClick={() => onDelete(media)}
          aria-label={`Delete ${media.filename}`}
          className="absolute right-1.5 top-1.5 flex h-7 w-7 items-center justify-center rounded-lg bg-surface/90 text-faint opacity-0 transition-opacity hover:text-danger focus-visible:opacity-100 group-hover:opacity-100"
        >
          <Trash2 size={13} />
        </button>
      </div>
      {/* meta */}
      <div className="flex min-w-0 flex-1 flex-col gap-1 px-3.5 py-3">
        <p className="truncate text-[13px] font-semibold text-ink" title={media.filename}>
          {media.filename}
        </p>
        <p className="font-mono text-2xs text-faint">
          {media.mime} · {formatBytes(media.size)}
        </p>
        <div className="mt-auto flex items-center justify-between gap-2 pt-1.5">
          <Badge tone={media.kind === "image" ? "active" : media.kind === "document" ? "dim" : "info"}>
            {media.kind}
          </Badge>
          <span className="truncate font-mono text-2xs text-faint" title={media.session_id}>
            {media.session_id.slice(0, 8)}…
          </span>
        </div>
      </div>
    </div>
  );
}

function FilePreview({
  media,
  attachment,
  hover,
  onOpenImage,
}: {
  media: MediaView;
  attachment: AttachmentRef;
  hover: boolean;
  onOpenImage: (attachment: AttachmentRef) => void;
}) {
  const isImage = media.kind === "image";
  const { url, error } = useMediaUrl(isImage ? attachment : null);
  if (isImage) {
    if (error) {
      return (
        <span className="flex h-full w-full items-center justify-center text-xs text-danger">
          failed to load
        </span>
      );
    }
    return (
      <button
        type="button"
        onClick={() => onOpenImage(attachment)}
        className="h-full w-full"
        aria-label={`Preview ${media.filename}`}
      >
        {url ? (
          <img
            src={url}
            alt={media.filename}
            loading="lazy"
            className={`h-full w-full object-cover transition-transform ${hover ? "scale-105" : ""}`}
          />
        ) : (
          <span className="flex h-full w-full animate-pulse items-center justify-center bg-raised-soft" />
        )}
      </button>
    );
  }
  return (
    <span className="flex h-14 w-14 items-center justify-center rounded-2xl border border-line bg-surface text-copper">
      <DocumentIcon mime={media.mime} name={media.filename} size={24} />
    </span>
  );
}
