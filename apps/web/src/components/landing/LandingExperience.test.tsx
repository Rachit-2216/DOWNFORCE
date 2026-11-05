import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { LandingExperience } from "./LandingExperience";

vi.mock("next/dynamic", () => ({
  default: () =>
    function Scene() {
      return <div data-testid="car-canvas" />;
    },
}));
vi.mock("@/components/product/OnlineTools", () => ({
  OnlineTools: () => <div>Online race tools</div>,
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("keeps one car stage through the downstream handoff, before the real tools", () => {
  const { container } = render(<LandingExperience />);
  const machine = container.querySelector(".public-machine")!;
  const handoff = screen.getByRole("region", {
    name: "Every part. One picture.",
  });
  expect(machine).toContainElement(handoff);
  expect(container.querySelectorAll(".machine-stage")).toHaveLength(1);
  expect(
    handoff.compareDocumentPosition(screen.getByText("Online race tools")) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
});

it("starts assembled even after jumping directly to the downstream section", () => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
    {} as WebGLRenderingContext,
  );
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
  vi.stubGlobal("innerHeight", 900);
  let frame: FrameRequestCallback | undefined;
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn((callback: FrameRequestCallback) => {
      frame = callback;
      return 1;
    }),
  );
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  const bounds = vi
    .spyOn(HTMLElement.prototype, "getBoundingClientRect")
    .mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("anatomy-container")
        ? ({ top: -5400, bottom: 0, height: 5400 } as DOMRect)
        : ({ top: 0, bottom: 900, height: 900 } as DOMRect);
    });
  const { container } = render(<LandingExperience />);
  expect(container.querySelector(".public-machine")).toHaveAttribute(
    "data-chapter",
    "departure",
  );
  expect(container.querySelector(".public-machine")).toHaveStyle(
    "--machine-departure: 1",
  );
  bounds.mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("anatomy-container")
      ? ({ top: 900, bottom: 6300, height: 5400 } as DOMRect)
      : ({ top: 0, bottom: 900, height: 900 } as DOMRect);
  });
  act(() => {
    window.dispatchEvent(new Event("scroll"));
    frame?.(0);
  });
  expect(container.querySelector(".public-machine")).toHaveAttribute(
    "data-chapter",
    "transfer",
  );
  expect(container.querySelector(".public-machine")).toHaveStyle(
    "--machine-departure: 0",
  );
});
