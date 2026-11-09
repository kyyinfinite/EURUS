
import { FileBox, FlaskConical } from "lucide-react";
import type { MissionArtifactView, MissionVerificationView } from "../../lib/types";
import { formatBytes } from "../../lib/format";
import { Badge } from "../ui";

const VERIFICATION_TONES: Record<string, "ok" | "warn" | "danger" | "info" | "dim"> = {
  passed: "ok",
  failed: "danger",
  timed_out: "danger",
  skipped: "warn",
  not_available: "warn",
};

export interface ArtifactPanelProps {
  artifacts: MissionArtifactView[];
  verifications: MissionVerificationView[];
}

export function ArtifactPanel({ artifacts, verifications }: ArtifactPanelProps) {
  return (
    <div className="space-y-3">
      <section>
        <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-faint">
          <FileBox size={13} /> Artifacts
        </h3>
        {artifacts.length === 0 ? (
          <p className="text-xs text-faint">No artifacts published.</p>
        ) : (
          <div className="space-y-1.5">
            {artifacts.map((artifact) => (
              <div
                key={artifact.artifact_id}
                className="rounded-lg border border-line-soft px-2.5 py-2"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="info">{artifact.artifact_type}</Badge>
                  <span className="font-mono text-2xs text-faint">
                    v{artifact.version} · {artifact.media_type} · {formatBytes(artifact.size_bytes)}
                  </span>
                  {artifact.assignment_id && (
                    <span className="font-mono text-2xs text-faint">
                      by {artifact.assignment_id.slice(0, 8)}
                    </span>
                  )}
                </div>
                <div className="mt-1 truncate font-mono text-2xs text-faint">
                  sha256:{artifact.content_hash.slice(0, 24)}…
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
      <section>
        <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-faint">
          <FlaskConical size={13} /> Verification evidence
        </h3>
        {verifications.length === 0 ? (
          <p className="text-xs text-faint">
            No checks recorded — claims are never evidence (ADR-006).
          </p>
        ) : (
          <div className="space-y-1.5">
            {verifications.map((verification, index) => (
              <div
                key={`${verification.task_id}-${verification.check_name}-${index}`}
                className="rounded-lg border border-line-soft px-2.5 py-2"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={VERIFICATION_TONES[verification.status] ?? "dim"}>
                    {verification.status}
                  </Badge>
                  <span className="font-mono text-xs text-ink">{verification.check_name}</span>
                  <span className="font-mono text-2xs text-faint">{verification.category}</span>
                  {verification.exit_code !== null && (
                    <span className="font-mono text-2xs text-faint">
                      exit {verification.exit_code}
                    </span>
                  )}
                  {verification.duration_ms !== null && (
                    <span className="font-mono text-2xs text-faint">
                      {verification.duration_ms} ms
                    </span>
                  )}
                </div>
                {verification.command_argv.length > 0 && (
                  <div className="mt-1 truncate font-mono text-2xs text-faint">
                    $ {verification.command_argv.join(" ")}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
