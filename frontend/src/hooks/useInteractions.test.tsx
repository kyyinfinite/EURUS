import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAutoScroll } from "./useInteractions";

class MockResizeObserver {
  static instances: MockResizeObserver[] = [];
  callback: ResizeObserverCallback;
  observed: Element[] = [];

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    MockResizeObserver.instances.push(this);
  }

  observe(target: Element): void {
    this.observed.push(target);
  }

  unobserve(): void {}

  disconnect(): void {}

  trigger(): void {
    this.callback([], this as unknown as ResizeObserver);
  }
}

function ScrollHarness() {
  const { ref } = useAutoScroll<HTMLDivElement>();
  return <div ref={ref}><div data-testid="content" /></div>;
}

afterEach(() => {
  vi.unstubAllGlobals();
  MockResizeObserver.instances = [];
});

describe("useAutoScroll", () => {
  it("observes conversation content and follows its size while pinned", () => {
    vi.stubGlobal("ResizeObserver", MockResizeObserver);
    const view = render(<ScrollHarness />);
    const container = view.container.firstElementChild as HTMLDivElement;
    const content = view.getByTestId("content");
    Object.defineProperties(container, {
      scrollHeight: { configurable: true, value: 400 },
      scrollTop: { configurable: true, writable: true, value: 0 },
      clientHeight: { configurable: true, value: 100 },
    });

    act(() => MockResizeObserver.instances[0]?.trigger());

    expect(MockResizeObserver.instances[0]?.observed).toContain(container);
    expect(MockResizeObserver.instances[0]?.observed).toContain(content);
    expect(container.scrollTop).toBe(400);
    view.unmount();
  });
});
