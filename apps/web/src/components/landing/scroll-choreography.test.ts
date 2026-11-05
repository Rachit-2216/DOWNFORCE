import { describe, expect, it } from "vitest";

import { landingPoses, sceneForComponent } from "./car-motion";
import { scrollChapter, selectionAtChapter } from "./scroll-choreography";

describe("the continuous anatomy scroll tour", () => {
  it("keeps the whole car assembled during transfer and after arrival", () => {
    for (const progress of [-1, 0, 0.07, 0.139, 0.14, 0.2, 0.239]) {
      expect(landingPoses[scrollChapter(progress).scene].mode).toBe(
        "assembled",
      );
    }
    expect(scrollChapter(0.07).phase).toBe("transfer");
    expect(scrollChapter(0.14).phase).toBe("settled");
    expect(scrollChapter(0.239).phase).toBe("settled");
    expect(scrollChapter(0.24).scene).toBe("exploded");
  });

  it("shows all eight systems in order, then reassembles before products", () => {
    for (let index = 0; index < 8; index += 1) {
      const chapter = scrollChapter(0.34 + (index + 0.5) * 0.075);
      expect(chapter.phase).toBe("spotlight");
      expect(chapter.scene).toBe(sceneForComponent(index));
      expect(chapter.partIndex).toBe(index);
    }
    expect(scrollChapter(0.94).scene).toBe("reassemble");
    expect(scrollChapter(2).scene).toBe("reassemble");
  });

  it("is reversible and cannot explode during backwards transfer", () => {
    const progress = [0.99, 0.8, 0.45, 0.28, 0.2, 0.07, 0];
    expect(progress.map((value) => scrollChapter(value).scene)).toEqual([
      "reassemble",
      sceneForComponent(6),
      sceneForComponent(1),
      "exploded",
      "reassemble",
      "hero",
      "hero",
    ]);
  });

  it("preserves a manual selection within a chapter and resumes only at a boundary", () => {
    const manual = sceneForComponent(7);
    const current = scrollChapter(0.4);
    expect(selectionAtChapter(manual, current.key, scrollChapter(0.405))).toBe(
      manual,
    );
    expect(selectionAtChapter(manual, current.key, scrollChapter(0.43))).toBe(
      sceneForComponent(1),
    );
  });

  it("holds the rebuilt car before carrying it into the next section", () => {
    expect(scrollChapter(0.94, 0).scene).toBe("reassemble");
    expect(scrollChapter(1.02, 0.34).scene).toBe("reassemble");
    expect(scrollChapter(1.1, 0.35).phase).toBe("departure");
    expect(scrollChapter(1.1, 0.35).scene).toBe("handoff");
    expect(landingPoses[scrollChapter(1.2, 1).scene].mode).toBe("assembled");
  });

  it("reverses the departure without exploding the rebuilt car", () => {
    expect(
      [1, 0.6, 0.35, 0.34, 0].map(
        (departure) => scrollChapter(1, departure).scene,
      ),
    ).toEqual(["handoff", "handoff", "handoff", "reassemble", "reassemble"]);
  });
});
