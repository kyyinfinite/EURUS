import { describe, expect, it, vi } from "vitest";
import { createGatewayEventBatcher } from "./gatewayBatch";
import type { GatewayEvent } from "./types";

const event: GatewayEvent = {
  type: "sessions_changed",
};

describe("createGatewayEventBatcher", () => {
  it("groups events into one scheduled batch", () => {
    const scheduled: { callback?: () => void } = {};
    const consume = vi.fn();
    const batcher = createGatewayEventBatcher(consume, {
      request: (next) => {
        scheduled.callback = next;
        return 1;
      },
      cancel: vi.fn(),
    });

    batcher.push(event);
    batcher.push(event);
    expect(consume).not.toHaveBeenCalled();
    scheduled.callback?.();
    expect(consume).toHaveBeenCalledOnce();
    expect(consume).toHaveBeenCalledWith([event, event]);
  });

  it("flushes synchronously and discards pending events on dispose", () => {
    const cancel = vi.fn();
    const consume = vi.fn();
    const batcher = createGatewayEventBatcher(consume, {
      request: () => 7,
      cancel,
    });

    batcher.push(event);
    batcher.flush();
    expect(consume).toHaveBeenCalledWith([event]);
    batcher.push(event);
    batcher.dispose();
    batcher.flush();
    expect(cancel).toHaveBeenCalledWith(7);
    expect(consume).toHaveBeenCalledOnce();
  });
});
