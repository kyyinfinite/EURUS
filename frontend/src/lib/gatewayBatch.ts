import type { GatewayEvent } from "./types";

export interface GatewayBatchScheduler {
  request: (callback: () => void) => number;
  cancel: (handle: number) => void;
}

export function createGatewayEventBatcher(
  consume: (events: GatewayEvent[]) => void,
  scheduler: GatewayBatchScheduler,
): { push: (event: GatewayEvent) => void; flush: () => void; dispose: () => void } {
  let queue: GatewayEvent[] = [];
  let frame: number | null = null;
  let disposed = false;

  const flush = () => {
    if (frame !== null) {
      scheduler.cancel(frame);
      frame = null;
    }
    if (disposed || queue.length === 0) return;
    const batch = queue;
    queue = [];
    consume(batch);
  };

  return {
    push: (event) => {
      if (disposed) return;
      queue.push(event);
      if (frame === null) frame = scheduler.request(flush);
    },
    flush,
    dispose: () => {
      disposed = true;
      if (frame !== null) scheduler.cancel(frame);
      frame = null;
      queue = [];
    },
  };
}
