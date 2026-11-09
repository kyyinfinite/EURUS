import { beforeEach, describe, expect, it } from "vitest";
import { useUI } from "./ui";

describe("thinking display preferences", () => {
  beforeEach(() => {
    localStorage.clear();
    useUI.setState({ thinkingPanelExpanded: true, thinkingTimelineExpanded: false });
  });

  it("persists the thinking panel expansion preference", () => {
    useUI.getState().setThinkingPanelExpanded(false);
    expect(useUI.getState().thinkingPanelExpanded).toBe(false);
    expect(localStorage.getItem("eurus.ui.thinkingPanelExpanded")).toBe("0");
  });

  it("persists the activity timeline detail preference", () => {
    useUI.getState().setThinkingTimelineExpanded(true);
    expect(useUI.getState().thinkingTimelineExpanded).toBe(true);
    expect(localStorage.getItem("eurus.ui.thinkingTimelineExpanded")).toBe("1");
  });
});
