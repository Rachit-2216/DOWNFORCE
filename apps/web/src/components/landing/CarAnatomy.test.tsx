import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Euler, PerspectiveCamera, Vector3 } from "three";

import { componentSpotlights } from "@/content/landing";
import { CarAnatomy } from "./CarAnatomy";
import {
  heroScaleForViewport,
  landingPoses,
  sceneForComponent,
  type CarScene,
} from "./car-motion";

afterEach(cleanup);

describe("CarAnatomy", () => {
  it("still labels the departing car as assembled", () => {
    render(
      <CarAnatomy
        scene="handoff"
        onSceneChange={vi.fn()}
        tourPhase="departure"
      />,
    );
    expect(screen.getByRole("button", { name: "Assemble" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Explode" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });
  it("removes departing anatomy controls from keyboard focus until the study returns", () => {
    const { container, rerender } = render(
      <CarAnatomy
        scene="handoff"
        onSceneChange={vi.fn()}
        tourPhase="departure"
      />,
    );
    expect(container.querySelector(".anatomy-section")).toHaveAttribute(
      "inert",
    );
    rerender(
      <CarAnatomy
        scene="reassemble"
        onSceneChange={vi.fn()}
        tourPhase="complete"
      />,
    );
    expect(container.querySelector(".anatomy-section")).not.toHaveAttribute(
      "inert",
    );
  });
  it("describes the actual manual selection instead of a stale scroll phase", () => {
    const { rerender } = render(
      <CarAnatomy
        scene="reassemble"
        onSceneChange={vi.fn()}
        tourPhase="spotlight"
      />,
    );
    expect(
      screen.getByText("Whole machine assembled. Scroll to continue."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/SYSTEM 00/)).not.toBeInTheDocument();
    rerender(
      <CarAnatomy
        scene="spotlight-rear-wing"
        onSceneChange={vi.fn()}
        tourPhase="complete"
      />,
    );
    expect(screen.getByText(/SYSTEM 08 \/ 08/)).toBeInTheDocument();
    expect(screen.queryByText(/Back together/)).not.toBeInTheDocument();
    rerender(
      <CarAnatomy
        scene="exploded"
        onSceneChange={vi.fn()}
        tourPhase="complete"
      />,
    );
    expect(
      screen.getByText("Eight connected systems. Scroll to look closer."),
    ).toBeInTheDocument();
  });

  it("never renders the baked-checkerboard component PNGs in a static fallback", () => {
    const { container } = render(
      <CarAnatomy scene="spotlight-rear-wing" onSceneChange={vi.fn()} />,
    );
    expect(
      container.querySelector('img[src*="/components/"]'),
    ).not.toBeInTheDocument();
    expect(
      container.querySelector('img[src="/images/rb22-static.png"]'),
    ).toBeInTheDocument();
    expect(screen.getByText(/Static whole-car reference/)).toBeInTheDocument();
  });
  it("makes a settled arrival assembled, with a visible tour caption and exit", () => {
    render(
      <CarAnatomy
        scene="reassemble"
        onSceneChange={vi.fn()}
        tourPhase="settled"
      />,
    );
    expect(
      screen.getByText("In position. Scroll to separate the systems."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /skip to online tools/i }),
    ).toHaveAttribute("href", "#online-tools");
    expect(screen.getByRole("button", { name: "Assemble" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
  it("keeps the car centered in its dedicated viewport and all part copy on the right", () => {
    expect(landingPoses.hero.rootPosition[0]).toBe(0);
    componentSpotlights.forEach((_, index) => {
      const pose = landingPoses[sceneForComponent(index)];
      expect(pose.textSide).toBe("right");
      expect(pose.rootPosition[0]).toBe(0);
    });
  });
  it("gives the wide hero more presence without cropping the car at either desktop size", () => {
    const compactScale = heroScaleForViewport(614, 550);
    const wideScale = heroScaleForViewport(864, 640);
    expect(compactScale).toBeCloseTo(0.96, 2);
    expect(wideScale / compactScale).toBeGreaterThanOrEqual(1.35);
    expect(wideScale).toBeLessThanOrEqual(1.34);

    // The supplied GLB measures 1.972 × 1.174 × 5.268 after rebasing.
    // Project its bounding corners through a full hero turn, not just one pose.
    for (const [width, height] of [
      [614, 550],
      [864, 640],
      [864, 450],
    ]) {
      const camera = new PerspectiveCamera(36, width / height, 0.1, 120);
      camera.position.set(...landingPoses.hero.cameraPosition);
      camera.lookAt(new Vector3(...landingPoses.hero.cameraTarget));
      camera.updateMatrixWorld();
      const scale = heroScaleForViewport(width, height);
      for (let yaw = 0; yaw < Math.PI * 2; yaw += Math.PI / 36) {
        const rotation = new Euler(0.04, yaw - 0.62, -0.018);
        for (const x of [-0.986, 0.986])
          for (const y of [-0.587, 0.587])
            for (const z of [-2.634, 2.634]) {
              const point = new Vector3(x, y, z)
                .multiplyScalar(scale)
                .applyEuler(rotation)
                .add(new Vector3(...landingPoses.hero.rootPosition))
                .project(camera);
              expect(Math.abs(point.x)).toBeLessThan(0.94);
              expect(Math.abs(point.y)).toBeLessThan(0.94);
            }
      }
    }
    expect(heroScaleForViewport(480, 640)).toBeLessThan(compactScale);
  });
  it("exposes all eight parts as native keyboard buttons with scene selection", () => {
    const onSceneChange = vi.fn();
    const { rerender } = render(
      <CarAnatomy scene="exploded" onSceneChange={onSceneChange} />,
    );
    const index = screen.getByRole("group", { name: "Choose a car component" });
    expect(within(index).getAllByRole("button")).toHaveLength(8);
    componentSpotlights.forEach((part, index) => {
      const button = screen.getByRole("button", { name: part.title });
      expect(button.tagName).toBe("BUTTON");
      expect(button).toHaveAttribute("type", "button");
      button.focus();
      expect(button).toHaveFocus();
      fireEvent.click(button);
      expect(onSceneChange).toHaveBeenLastCalledWith(sceneForComponent(index));
      rerender(
        <CarAnatomy
          scene={sceneForComponent(index)}
          onSceneChange={onSceneChange}
        />,
      );
      expect(screen.getByRole("button", { name: part.title })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      expect(
        screen.getByRole("heading", { name: part.title }),
      ).toBeInTheDocument();
      expect(screen.getByText(part.body)).toBeInTheDocument();
      expect(
        within(
          screen.getByRole("group", { name: "Choose a car component" }),
        ).getAllByRole("button", { pressed: true }),
      ).toHaveLength(1);
    });
  });

  it("assembles and explodes without requiring WebGL", () => {
    function Harness() {
      const [scene, setScene] = useState<CarScene>("exploded");
      return <CarAnatomy scene={scene} onSceneChange={setScene} />;
    }
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Assemble" }));
    expect(screen.getByRole("button", { name: "Assemble" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Explode" }));
    expect(screen.getByRole("button", { name: "Explode" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      screen.getByText(/engineering-inspired visual/i),
    ).toBeInTheDocument();
    expect(document.querySelector("canvas")).not.toBeInTheDocument();
  });
});
