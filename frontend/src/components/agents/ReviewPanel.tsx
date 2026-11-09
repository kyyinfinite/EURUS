
import { CircleAlert, MessageSquareWarning } from "lucide-react";
import type { MissionFindingView, MissionReviewView } from "../../lib/types";
import { Badge } from "../ui";

const SEVERITY_TONES: Record<string, "ok" | "warn" | "danger" | "info" | "dim"> = {
  blocker: "danger",
  critical: "danger",
  major: "warn",
  minor: "info",
  info: "dim",
};

const BLOCKING = new Set(["blocker", "critical", "major"]);

export interface ReviewPanelProps {
  reviews: MissionReviewView[];
}

export function ReviewPanel({ reviews }: ReviewPanelProps) {
  return (
    <div className="space-y-3">
      {reviews.length === 0 && (
        <p className="text-xs text-faint">No reviews recorded for this mission yet.</p>
      )}
      {reviews.map((review) => {
        const blocking = review.findings.filter((finding) => BLOCKING.has(finding.severity));
        return (
          <div key={review.review_id} className="rounded-lg border border-line-soft p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge
                tone={
                  review.status === "approved"
                    ? "ok"
                    : review.status === "rejected"
                      ? "danger"
                      : "warn"
                }
              >
                {review.status}
              </Badge>
              <span className="font-mono text-2xs text-faint">cycle {review.cycle}</span>
              {blocking.length > 0 && (
                <span className="flex items-center gap-1 text-2xs text-danger">
                  <CircleAlert size={12} /> {blocking.length} blocking finding(s)
                </span>
              )}
            </div>
            {review.summary && (
              <p className="mt-1.5 whitespace-pre-wrap text-xs leading-relaxed text-muted">
                {review.summary}
              </p>
            )}
            {review.findings.length > 0 && (
              <ul className="mt-2 space-y-1.5">
                {review.findings.map((finding) => (
                  <FindingRow key={finding.finding_id} finding={finding} />
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}

function FindingRow({ finding }: { finding: MissionFindingView }) {
  const blocking = BLOCKING.has(finding.severity);
  return (
    <li className="rounded-md border border-line-soft bg-canvas px-2.5 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={SEVERITY_TONES[finding.severity] ?? "dim"}>{finding.severity}</Badge>
        <span className="font-mono text-2xs text-faint">{finding.category}</span>
        {finding.location && (
          <span className="truncate font-mono text-2xs text-muted">{finding.location}</span>
        )}
        {finding.disposition !== "open" && (
          <span className="font-mono text-2xs text-faint">{finding.disposition}</span>
        )}
      </div>
      {finding.evidence && (
        <p className="mt-1 flex items-start gap-1 text-xs leading-relaxed text-muted">
          <MessageSquareWarning size={12} className="mt-0.5 shrink-0 text-faint" />
          {finding.evidence}
        </p>
      )}
      {finding.suggested_remediation && (
        <p className="mt-0.5 text-2xs italic text-faint">fix: {finding.suggested_remediation}</p>
      )}
      {finding.revision_task_id && (
        <p className="mt-0.5 font-mono text-2xs text-copper">
          revision {finding.revision_task_id.slice(0, 8)}
        </p>
      )}
      {blocking && finding.disposition === "open" && (
        <p className="mt-0.5 text-2xs font-semibold text-danger">
          blocks acceptance until resolved
        </p>
      )}
    </li>
  );
}
