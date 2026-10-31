/** Providers — status, key availability, model discovery (no secrets shown). */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import type { ModelInfoView, ProviderView } from "../lib/types";
import { Badge, Button, Card, Empty, LoadingState, PageHeader, StatusDot } from "../components/ui";

export default function Providers() {
  const query = useQuery({
    queryKey: ["providers"],
    queryFn: () => api.get<ProviderView[]>("/api/providers"),
    refetchInterval: 8000,
  });
  const providers = query.data ?? [];
  const ready = providers.filter((provider) => provider.status === "ready").length;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Providers"
        subtitle={`${providers.length} configured · ${ready} ready — API keys stay in your environment`}
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-4 pb-28 sm:p-5 md:pb-12">
        {query.isLoading ? (
          <LoadingState label="Loading providers…" />
        ) : providers.length === 0 ? (
          <Empty>No providers configured. Run `eurus setup provider` or edit the config.</Empty>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {providers.map((provider) => (
              <ProviderCard key={provider.name} provider={provider} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ProviderCard({ provider }: { provider: ProviderView }) {
  const queryClient = useQueryClient();
  const [models, setModels] = useState<ModelInfoView[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const discover = useMutation({
    mutationFn: () =>
      api.post<{ provider: string; error: string; models: ModelInfoView[] }>(
        `/api/providers/${provider.name}/discover`,
      ),
    onSuccess: (result) => {
      setModels(result.models);
      setError(result.error || null);
      void queryClient.invalidateQueries({ queryKey: ["providers"] });
    },
    onError: (exc) => setError(exc.message),
  });

  const tone = provider.status === "ready" ? "ok" : provider.status === "missing key" ? "warn" : "dim";

  return (
    <Card
      title={
        <span className="flex items-center gap-2 font-mono text-xs normal-case tracking-normal text-ink">
          <StatusDot tone={tone} />
          {provider.name}
          {provider.builtin && <Badge tone="bronze">builtin</Badge>}
        </span>
      }
    >
      <dl className="space-y-1 text-2xs">
        <Row label="protocol" value={provider.protocol} />
        <Row label="base url" value={provider.base_url} />
        <Row label="key env" value={provider.api_key_env || "—"} />
        <Row label="status" value={<Badge tone={tone}>{provider.status}</Badge>} />
      </dl>
      <div className="mt-3 border-t border-line-soft pt-3">
        <Button onClick={() => discover.mutate()} disabled={discover.isPending} size="sm">
          <Download size={11} /> List models
        </Button>
        {discover.isPending && (
          <span className="ml-2 inline-flex items-center gap-1.5 text-2xs text-dim">
            <RefreshCw size={11} className="animate-spin" /> querying…
          </span>
        )}
        {error && <p className="mt-2 text-2xs text-danger">{error}</p>}
        {models && models.length > 0 && (
          <ul className="mt-2 max-h-40 space-y-0.5 overflow-y-auto rounded-lg border border-line-soft bg-canvas p-2">
            {models.map((model) => (
              <li key={model.id} className="flex items-center justify-between font-mono text-2xs">
                <span className="truncate text-ink">{model.id}</span>
                {model.context > 0 && (
                  <span className="ml-2 shrink-0 text-faint">{model.context.toLocaleString()} ctx</span>
                )}
              </li>
            ))}
          </ul>
        )}
        {models && models.length === 0 && !error && (
          <p className="mt-2 text-2xs text-faint">No chat models reported.</p>
        )}
      </div>
    </Card>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[84px_1fr] items-start gap-2">
      <dt className="pt-0.5 text-faint">{label}</dt>
      <dd className="min-w-0 break-all font-mono text-ink" title={typeof value === "string" ? value : undefined}>
        {value}
      </dd>
    </div>
  );
}
