"use client";

/* eslint-disable @next/next/no-img-element -- native transparent images keep anatomy available without WebGL */

import { componentSpotlights } from "@/content/landing";
import type { ReactNode } from "react";

import { landingPoses, sceneForComponent, type CarScene } from "./car-motion";
import type { TourPhase } from "./scroll-choreography";

export function CarAnatomy({
  scene,
  onSceneChange,
  tourPhase,
  stage,
}: {
  scene: CarScene;
  onSceneChange: (scene: CarScene) => void;
  tourPhase?: TourPhase;
  stage?: ReactNode;
}) {
  const selectedIndex = componentSpotlights.findIndex(
    (_, index) => sceneForComponent(index) === scene,
  );
  const selected =
    selectedIndex >= 0 ? componentSpotlights[selectedIndex] : undefined;
  const assembled = landingPoses[scene].mode === "assembled";
  const caption = selected
    ? `SYSTEM ${String(selectedIndex + 1).padStart(2, "0")} / 08${tourPhase ? " · Scroll to continue" : " · Selected system"}`
    : !assembled
      ? tourPhase
        ? "Eight connected systems. Scroll to look closer."
        : "Exploded overview. Select a system to look closer."
      : !tourPhase
        ? "Select a system. Explore at your own pace."
        : tourPhase === "transfer"
          ? "The whole machine. Moving into position."
          : tourPhase === "settled"
            ? "In position. Scroll to separate the systems."
            : tourPhase === "complete"
              ? "Back together. Continue to the race workspace."
              : "Whole machine assembled. Scroll to continue.";

  return (
    <section
      className="anatomy-section"
      id="anatomy"
      aria-labelledby="anatomy-title"
      inert={tourPhase === "departure"}
    >
      <div className="anatomy-heading">
        <p className="public-eyebrow">01 / INSIDE THE MACHINE</p>
        <h2 id="anatomy-title">One machine. Eight connected systems.</h2>
        <p>Start with the whole. Scroll the study, or select a system.</p>
      </div>
      <div className="anatomy-tour-status">
        <p>{caption}</p>
        <a href="#online-tools">
          Skip to online tools <span aria-hidden="true">↓</span>
        </a>
        <div className="anatomy-tour-progress" aria-hidden="true">
          <span />
        </div>
      </div>
      <div className="anatomy-toolbar" role="group" aria-label="Car assembly">
        <button
          type="button"
          aria-pressed={!assembled && !selected}
          onClick={() => onSceneChange("exploded")}
        >
          Explode
        </button>
        <button
          type="button"
          aria-pressed={assembled}
          onClick={() => onSceneChange("reassemble")}
        >
          Assemble
        </button>
      </div>
      <div className="anatomy-static-slot" aria-hidden="true">
        {stage ?? (
          <img
            className="anatomy-static-subject"
            src="/images/rb22-static.png"
            alt=""
          />
        )}
      </div>
      <div className="anatomy-detail" aria-live="polite" aria-atomic="true">
        <p className="public-eyebrow">
          {selected
            ? `${String(selectedIndex + 1).padStart(2, "0")} / ${selected.system}`
            : "RB22 / SYSTEM OVERVIEW"}
        </p>
        <h3>
          {selected?.title ??
            (assembled ? "Built to work together." : "Every part has a role.")}
        </h3>
        <p>
          {selected?.body ??
            "Airflow, structure and mechanical grip meet in one tightly connected machine. Explore the assemblies, then bring the car back together."}
        </p>
        <ul className="anatomy-tags" aria-label="Component characteristics">
          {(selected?.tags ?? ["AIRFLOW", "STRUCTURE", "MECHANICAL GRIP"]).map(
            (tag) => (
              <li key={tag}>{tag}</li>
            ),
          )}
        </ul>
        <p className="anatomy-disclaimer">
          Engineering-inspired visual, not a vehicle-physics simulation.
        </p>
        {!tourPhase && !stage ? (
          <p className="anatomy-disclaimer">
            Static whole-car reference. The selected system is described above.
          </p>
        ) : null}
      </div>
      <div
        className="anatomy-index"
        role="group"
        aria-label="Choose a car component"
      >
        {componentSpotlights.map((part, index) => (
          <button
            key={part.group}
            type="button"
            aria-label={part.title}
            aria-pressed={selectedIndex === index}
            onClick={() => onSceneChange(sceneForComponent(index))}
          >
            <span aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
            {part.title}
            <span className="anatomy-index__arrow" aria-hidden="true">
              ↗
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}
