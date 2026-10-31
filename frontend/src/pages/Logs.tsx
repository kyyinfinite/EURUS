/** Logs & Diagnostics — in-process ring buffer from the gateway runtime,
 *  with level filtering, text search and follow mode. */

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { LogRecordView } from "../lib/types";
import { Search } from "lucide-react";
import { Badge, Button, Empty, PageHeader } from "../components/ui";

const LEVELS = ["INFO", "WARNING", "ERROR", "CRITICAL", "DEBUG"] as const;
type Level = (typeof LEVELS)[number];
const LEVEL_TONE: Record<string, "ok" | "warn" | "danger" | "dim"> = {
  INFO: "dim",
  WARNING: "warn",
  ERROR: "danger",
  CRITICAL: "danger",
  DEBUG: "dim",
};

export default function Logs() {
  const [follow, setFollow] = useState(true);
  const [lastSequence, setLastSequence] = useState(0);
  const [records, setRecords] = useState<LogRecordView[]>([]);
  const [activeLevels, setActiveLevels] = useState<Set<Level>>(new Set(LEVELS));
  const [needle, setNeedle] = useState("");
  const boxRef = useRef<HTMLDivElement>(null);

  const query = useQuery({
    queryKey: ["logs", lastSequence],
    queryFn: () => api.get<LogRecordView[]>(`/api/logs?after_sequence=${lastSequence}&limit=400`),
    refetchInterval: 1500,
  });

  useEffect(() => {
    const incoming = query.data ?? [];
    if (incoming.length === 0) return;
    setRecords((prev) => [...prev, ...incoming].slice(-1000));
    setLastSequence(incoming[incoming.length - 1].sequence);
  }, [query.data]);

  useEffect(() => {
    if (follow && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [records, follow]);

  const filtered = useMemo(() => {
    const search = needle.trim().toLowerCase();
    return records.filter((record) => {
      if (!activeLevels.has(record.level as Level)) return false;
      if (!search) return true;
      return (
        record.message.toLowerCase().includes(search) || record.logger.toLowerCase().includes(search)
      );
    });
  }, [records, activeLevels, needle]);

  const toggleLevel = (level: Level) => {
    setActiveLevels((prev) => {
      const next = new Set(prev);
      if (next.has(level)) next.delete(level);
      else next.add(level);
      return next;
    });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Logs & Diagnostics"
        subtitle="Recent runtime records captured by the gateway (secrets redacted)"
        actions={
          <>
            <Button size="sm" variant={follow ? "sage" : "default"} onClick={() => setFollow(!follow)}>
              {follow ? "Following" : "Paused"}
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setRecords([]);
                setLastSequence(0);
              }}
            >
              Clear view
            </Button>
          </>
        }
      />
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-surface px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by level">
          {LEVELS.map((level) => (
            <button
              key={level}
              type="button"
              onClick={() => toggleLevel(level)}
              aria-pressed={activeLevels.has(level)}
              className={`rounded-full border px-2.5 py-1 font-mono text-2xs transition-colors ${
                activeLevels.has(level)
                  ? level === "ERROR" || level === "CRITICAL"
                    ? "border-danger/50 bg-danger/10 text-danger"
                    : level === "WARNING"
                      ? "border-warn/50 bg-warn/10 text-warn"
                      : "border-line bg-raised-soft text-dim"
                  : "border-line-soft bg-transparent text-faint hover:text-dim"
              }`}
            >
              {level}
            </button>
          ))}
        </div>
        <span className="relative ml-auto">
          <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
          <input
            value={needle}
            onChange={(event) => setNeedle(event.target.value)}
            placeholder="Filter text…"
            aria-label="Filter log text"
            className="w-40 py-1 pl-7 pr-2 text-2xs sm:w-56"
          />
        </span>
        <span className="font-mono text-2xs text-faint">{filtered.length} records</span>
      </div>
      <div className="min-h-0 flex-1 p-3 sm:p-5">
        <div
          ref={boxRef}
          className="h-full overflow-auto rounded-xl border border-line bg-canvas p-3 font-mono text-2xs leading-relaxed"
          role="log"
          aria-label="Log records"
          aria-live="off"
        >
          {filtered.length === 0 ? (
            <Empty>
              {records.length === 0
                ? "No log records yet — they appear as the runtime works."
                : "No records match the current filters."}
            </Empty>
          ) : (
            filtered.map((record) => (
              <div
                key={record.sequence}
                className="flex min-w-max gap-3 whitespace-nowrap py-[3px]"
              >
                <span className="shrink-0 text-muted">
                  {new Date(record.timestamp * 1000).toLocaleTimeString()}
                </span>
                <span className="w-[64px] shrink-0">
                  <Badge tone={LEVEL_TONE[record.level] ?? "dim"}>{record.level}</Badge>
                </span>
                <span className="hidden w-48 shrink-0 text-muted sm:block" title={record.logger}>
                  {record.logger}
                </span>
                <span className="text-dim" title={record.message}>
                  {record.message}
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
