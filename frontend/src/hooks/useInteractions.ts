/** Small interaction hooks shared across the workspace. */

import { useCallback, useEffect, useRef, useState } from "react";

/** Countdown that ticks once per second and returns remaining seconds (>= 0). */
export function useCountdown(seconds: number): number {
  const [remaining, setRemaining] = useState(Math.max(0, Math.round(seconds)));
  useEffect(() => {
    setRemaining(Math.max(0, Math.round(seconds)));
    const started = Date.now();
    const initial = Math.max(0, Math.round(seconds));
    const timer = window.setInterval(() => {
      setRemaining(Math.max(0, initial - Math.round((Date.now() - started) / 1000)));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [seconds]);
  return remaining;
}

/** Escape-key helper for lightweight popovers. */
export function useDismiss(onDismiss: () => void): void {
  const handler = useCallback(
    (event: KeyboardEvent) => {
      if (event.key === "Escape") onDismiss();
    },
    [onDismiss],
  );
  useEffect(() => {
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [handler]);
}

/** Global keyboard shortcut helper (ignores typing contexts by default). */
export function useHotkey(
  combo: { key: string; meta?: boolean; ctrl?: boolean; shift?: boolean },
  handler: () => void,
  { allowInInputs = false } = {},
): void {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.key !== combo.key.toLowerCase() && event.key !== combo.key) return;
      if (combo.meta && !(event.metaKey || event.ctrlKey)) return;
      if (combo.ctrl && !event.ctrlKey) return;
      if (combo.shift && !event.shiftKey) return;
      if (!combo.meta && !combo.ctrl && (event.metaKey || event.ctrlKey)) return;
      const target = event.target as HTMLElement | null;
      if (!allowInInputs && target) {
        const tag = target.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable) {
          return;
        }
      }
      event.preventDefault();
      handlerRef.current();
    };
    document.addEventListener("keydown", listener);
    return () => document.removeEventListener("keydown", listener);
  }, [combo.key, combo.meta, combo.ctrl, combo.shift, allowInInputs]);
}

/** Match a media query (SSR-safe, updates on change). */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches,
  );
  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    setMatches(mql.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/**
 * Auto-scroll pinning for streaming views: returns a ref for the scroll
 * container, whether the view is pinned to the bottom, an onScroll handler,
 * a `notify` callback for content changes and a `scrollToBottom` action.
 */
export function useAutoScroll<T extends HTMLElement>(threshold = 90) {
  const ref = useRef<T | null>(null);
  const pinned = useRef(true);
  const [showJump, setShowJump] = useState(false);

  const onScroll = useCallback(() => {
    const node = ref.current;
    if (!node) return;
    const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < threshold;
    pinned.current = atBottom;
    setShowJump(!atBottom);
  }, [threshold]);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "auto") => {
    const node = ref.current;
    if (!node) return;
    pinned.current = true;
    setShowJump(false);
    node.scrollTo({ top: node.scrollHeight, behavior });
  }, []);

  /** Call whenever streamed content grows. */
  const notify = useCallback(() => {
    const node = ref.current;
    if (node && pinned.current) {
      node.scrollTop = node.scrollHeight;
    }
  }, []);

  /** Reset pinning (e.g. when switching sessions). */
  const reset = useCallback(() => {
    pinned.current = true;
    setShowJump(false);
    const node = ref.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, []);

  return { ref, onScroll, notify, reset, scrollToBottom, showJump };
}
