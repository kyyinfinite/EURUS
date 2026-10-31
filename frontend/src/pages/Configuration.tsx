/** Configuration — interactive editor over the canonical TOML config.
 *
 * Validation is performed by EURUS itself (build_config through TomlManager);
 * the frontend never duplicates defaults or validation rules. Secrets (API
 * key env var *names*) are shown, but actual key values live only in the
 * environment / ~/.eurus/.env and are never exposed by the API.
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save } from "lucide-react";
import { api } from "../lib/api";
import type { ConfigPatchResponse, ConfigView } from "../lib/types";
import {
  Badge,
  Button,
  Card,
  Empty,
  LoadingState,
  PageHeader,
  Spinner,
} from "../components/ui";

const SECRET_KEYS = new Set(["api_key_env", "file"]);

function isEditableValue(_key: string, value: unknown): boolean {
  if (typeof value === "object" && value !== null) return false;
  return true;
}

export default function Configuration() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["config"],
    queryFn: () => api.get<ConfigView>("/api/config"),
  });

  const [section, setSection] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const data = query.data;
  useEffect(() => {
    if (data && !section) setSection(data.editable_sections[0] ?? null);
  }, [data, section]);

  const patch = useMutation({
    mutationFn: (input: { section: string; values: Record<string, unknown>; revision: number }) =>
      api.patch<ConfigPatchResponse>("/api/config", input),
    onSuccess: (response) => {
      setNotice(
        response.live_reload
          ? "Saved and applied to the running gateway."
          : `Saved. Live reload deferred: ${response.reload_error ?? "runs are active"} — restart the gateway to pick it up.`,
      );
      void queryClient.invalidateQueries({ queryKey: ["config"] });
    },
    onError: (exc) => setNotice(`Rejected: ${exc.message}`),
  });

  if (query.isLoading)
    return (
      <div className="flex h-full flex-col">
        <PageHeader title="Configuration" />
        <LoadingState label="Loading configuration…" />
      </div>
    );
  if (!data)
    return (
      <div className="flex h-full flex-col">
        <PageHeader title="Configuration" />
        <div className="p-6">
          <Empty>Configuration unavailable.</Empty>
        </div>
      </div>
    );

  const currentSection = section ?? data.editable_sections[0];
  const values = (data.config[currentSection] ?? {}) as Record<string, unknown>;
  const scalarEntries = Object.entries(values).filter(([key, value]) => isEditableValue(key, value));

  const save = () => {
    if (!currentSection) return;
    setNotice(null);
    const parsed: Record<string, unknown> = {};
    for (const [key, raw] of Object.entries(draft)) {
      if (raw === "") continue;
      const original = values[key];
      if (typeof original === "number") {
        const num = Number(raw);
        if (Number.isNaN(num)) {
          setNotice(`"${key}" must be a number`);
          return;
        }
        parsed[key] = num;
      } else if (typeof original === "boolean") {
        parsed[key] = raw === "true";
      } else if (Array.isArray(original)) {
        parsed[key] = raw
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean);
      } else {
        parsed[key] = raw;
      }
    }
    patch.mutate({ section: currentSection, values: parsed, revision: data.revision });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Configuration"
        subtitle={`${data.config_path} · revision ${data.revision}`}
      />
      <div className="grid min-h-0 flex-1 gap-4 overflow-y-auto p-4 sm:p-5 lg:grid-cols-[220px_1fr]">
        <nav className="space-y-1 lg:sticky lg:top-0 lg:self-start" aria-label="Config sections">
          {data.editable_sections.map((name) => (
            <button
              key={name}
              type="button"
              onClick={() => {
                setSection(name);
                setDraft({});
                setNotice(null);
              }}
              aria-current={name === currentSection ? "true" : undefined}
              className={`block w-full rounded-lg border-l-2 px-3 py-2 text-left font-mono text-xs transition-colors ${
                name === currentSection
                  ? "border-copper bg-copper-soft text-copper"
                  : "border-transparent text-dim hover:bg-raised-soft hover:text-ink"
              }`}
            >
              [{name}]
            </button>
          ))}
        </nav>

        <Card
          title={`[${currentSection}]`}
          actions={
            <Button
              variant="primary"
              size="sm"
              className="shrink-0 whitespace-nowrap"
              onClick={save}
              disabled={patch.isPending || Object.keys(draft).length === 0}
            >
              {patch.isPending ? <Spinner /> : <Save size={13} />} Save &amp; validate
            </Button>
          }
        >
          {notice && (
            <div
              className={`mb-3 rounded-lg border px-3 py-2 text-xs ${
                notice.startsWith("Rejected")
                  ? "border-danger/40 bg-danger/5 text-danger"
                  : "border-ok/40 bg-ok/5 text-ok"
              }`}
              role="status"
            >
              {notice}
            </div>
          )}
          {scalarEntries.length === 0 ? (
            <Empty>This section has no editable scalar values in the active config.</Empty>
          ) : (
            <div className="grid gap-2.5 sm:grid-cols-2">
              {scalarEntries.map(([key, value]) => {
                const secret = SECRET_KEYS.has(key);
                const isList = Array.isArray(value);
                const display = isList ? (value as unknown[]).join(", ") : String(value ?? "");
                return (
                  <label key={key} className="block">
                    <span className="mb-1 flex flex-wrap items-center gap-2 font-mono text-2xs text-dim">
                      {key}
                      {secret && <Badge tone="dim">env var name — value never shown</Badge>}
                      {typeof value === "boolean" && <Badge tone="dim">bool</Badge>}
                      {typeof value === "number" && <Badge tone="dim">number</Badge>}
                      {isList && <Badge tone="dim">comma-separated list</Badge>}
                    </span>
                    <input
                      className="w-full font-mono text-xs"
                      value={draft[key] ?? display}
                      placeholder={String(value ?? "")}
                      onChange={(event) =>
                        setDraft((prev) => ({ ...prev, [key]: event.target.value }))
                      }
                    />
                  </label>
                );
              })}
            </div>
          )}
          <p className="mt-4 border-t border-line-soft pt-3 text-2xs leading-relaxed text-faint">
            Values are merged into the user TOML and validated by EURUS itself before the file is
            written (atomic, mode 0600). Invalid values are rejected with the parser&apos;s own
            error. Nested objects (pricing models, provider entries) are managed via the Providers
            page or the CLI.
          </p>
        </Card>
      </div>
    </div>
  );
}
