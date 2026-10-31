import { resolveToken } from "./token";
import type { ApiErrorBody } from "./types";

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, detail: string) {
    super(detail);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${resolveToken()}`,
  };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let code = "error";
    let detail = `${response.status} ${response.statusText}`;
    try {
      const parsed = (await response.json()) as ApiErrorBody;
      if (parsed?.error) {
        code = parsed.error.code;
        detail = parsed.error.detail;
      }
    } catch {
      /* keep defaults */
    }
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent("eurus:unauthorized"));
    }
    throw new ApiError(response.status, code, detail);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  delete: <T>(path: string) => request<T>("DELETE", path),
};

/** Multipart upload with progress reporting (XMLHttpRequest gives us progress
 *  events; fetch does not). Uses the same auth header + 401 handling. */
export function upload<T>(
  path: string,
  form: FormData,
  onProgress?: (fraction: number) => void,
): Promise<T> {
  return uploadCancellable<T>(path, form, onProgress).promise;
}

/** Cancellable upload: call cancel() to abort an in-flight transfer. */
export function uploadCancellable<T>(
  path: string,
  form: FormData,
  onProgress?: (fraction: number) => void,
): { promise: Promise<T>; cancel: () => void } {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<T>((resolve, reject) => {
    xhr.open("POST", path);
    xhr.setRequestHeader("Authorization", `Bearer ${resolveToken()}`);
    xhr.responseType = "json";
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(event.loaded / event.total);
      }
    };
    xhr.onload = () => {
      const body = xhr.response as ApiErrorBody | T | null;
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as T);
        return;
      }
      if (xhr.status === 401) {
        window.dispatchEvent(new CustomEvent("eurus:unauthorized"));
      }
      const detail =
        body && typeof body === "object" && "error" in body && body.error
          ? `${body.error.detail}`
          : `${xhr.status} ${xhr.statusText}`;
      reject(new ApiError(xhr.status, "upload_failed", detail));
    };
    xhr.onabort = () => reject(new ApiError(0, "cancelled", "upload cancelled"));
    xhr.onerror = () => reject(new ApiError(0, "network", "upload failed — network error"));
    xhr.send(form);
  });
  return { promise, cancel: () => xhr.abort() };
}

/** Authenticated binary download; returns an object URL for <img>/<audio> use. */
export async function fetchBlobUrl(path: string): Promise<string> {
  const response = await fetch(path, {
    headers: { Authorization: `Bearer ${resolveToken()}` },
  });
  if (!response.ok) {
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent("eurus:unauthorized"));
    }
    throw new ApiError(response.status, "download_failed", `download failed (${response.status})`);
  }
  return URL.createObjectURL(await response.blob());
}
