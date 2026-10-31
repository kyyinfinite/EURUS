/** Token handling: the CLI prints the gateway URL with ?token=…; we persist it. */

const KEY = "eurus.gateway.token";

export function resolveToken(): string {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("token");
  if (fromUrl) {
    localStorage.setItem(KEY, fromUrl);
    params.delete("token");
    const next = params.toString();
    const clean = window.location.pathname + (next ? `?${next}` : "");
    window.history.replaceState({}, "", clean);
    return fromUrl;
  }
  return localStorage.getItem(KEY) ?? "";
}

export function hasToken(): boolean {
  return resolveToken().length > 0;
}

export function forgetToken(): void {
  localStorage.removeItem(KEY);
}
