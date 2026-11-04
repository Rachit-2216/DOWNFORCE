import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CarScene } from "./car-motion";
import { useDemandSnapshot } from "./use-demand-snapshot";

const { invalidate } = vi.hoisted(() => ({ invalidate: vi.fn() }));
vi.mock("@react-three/fiber", () => ({ useThree: () => invalidate }));
afterEach(() => {
  cleanup();
  invalidate.mockClear();
});

function Snapshot({
  scene,
  active = true,
  animate = false,
}: {
  scene: CarScene;
  active?: boolean;
  animate?: boolean;
}) {
  useDemandSnapshot(scene, active, animate);
  return null;
}

describe("demand-rendered component snapshots", () => {
  it("requests a frame for the loaded model and every distinct selected system", () => {
    const { rerender } = render(<Snapshot scene="spotlight-front-wing" />);
    expect(invalidate).toHaveBeenCalledTimes(1);
    rerender(<Snapshot scene="spotlight-rear-wing" />);
    expect(invalidate).toHaveBeenCalledTimes(2);
    rerender(<Snapshot scene="spotlight-floor" />);
    expect(invalidate).toHaveBeenCalledTimes(3);
    rerender(<Snapshot scene="spotlight-floor" />);
    expect(invalidate).toHaveBeenCalledTimes(3);
  });

  it("does not schedule continuous frames or render hidden snapshots", () => {
    const { rerender } = render(
      <Snapshot scene="spotlight-front-wing" active={false} />,
    );
    expect(invalidate).not.toHaveBeenCalled();
    rerender(<Snapshot scene="spotlight-rear-wing" active={false} />);
    expect(invalidate).not.toHaveBeenCalled();
    rerender(<Snapshot scene="spotlight-rear-wing" />);
    expect(invalidate).toHaveBeenCalledTimes(1);
    rerender(<Snapshot scene="spotlight-floor" animate />);
    expect(invalidate).toHaveBeenCalledTimes(1);
  });
});
