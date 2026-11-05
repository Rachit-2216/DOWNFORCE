import { sceneForComponent, type CarScene } from "./car-motion";

export type TourPhase =
  "transfer" | "settled" | "exploded" | "spotlight" | "complete" | "departure";
export type ScrollChapter = {
  key: string;
  phase: TourPhase;
  scene: CarScene;
  partIndex: number;
};

// Transfer finishes at .14. A separate settled chapter deliberately leaves
// scroll room between the car's arrival and the first separated assembly.
export function scrollChapter(progress: number, departure = 0): ScrollChapter {
  // Departure is measured from the end of the pinned study, independently of
  // its length. Reassembly gets a real hold before the next scene turns away.
  if (progress >= 1 && departure >= 0.35)
    return {
      key: "departure",
      phase: "departure",
      scene: "handoff",
      partIndex: -1,
    };
  if (progress < 0.14)
    return { key: "transfer", phase: "transfer", scene: "hero", partIndex: -1 };
  if (progress < 0.24)
    return {
      key: "settled",
      phase: "settled",
      scene: "reassemble",
      partIndex: -1,
    };
  if (progress < 0.34)
    return {
      key: "exploded",
      phase: "exploded",
      scene: "exploded",
      partIndex: -1,
    };
  if (progress < 0.94) {
    const partIndex = Math.min(7, Math.floor((progress - 0.34 + 1e-9) / 0.075));
    return {
      key: `part-${partIndex}`,
      phase: "spotlight",
      scene: sceneForComponent(partIndex),
      partIndex,
    };
  }
  return {
    key: "complete",
    phase: "complete",
    scene: "reassemble",
    partIndex: -1,
  };
}

export function selectionAtChapter(
  current: CarScene,
  previousKey: string,
  next: ScrollChapter,
): CarScene {
  return previousKey === next.key ? current : next.scene;
}
