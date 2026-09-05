import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useReplayController } from "./useReplayController";

describe("global replay controller", () => {
  it("clamps seek, speed and initial cursor to declared bounds", () => {
    const { result } = renderHook(() => useReplayController(100, 1_000, 50));
    expect(result.current.cursorMs).toBe(100);
    act(() => result.current.seekBy(2_000));
    expect(result.current.cursorMs).toBe(1_000);
    expect(result.current.authoritativeCursorMs).toBe(1_000);
    act(() => result.current.setSpeed(9));
    expect(result.current.speed).toBe(4);
  });

  it("pauses before scrubbing and reconciles the released cursor", () => {
    const { result } = renderHook(() => useReplayController(0, 1_000, 500));
    act(() => result.current.setPlaying(true));
    expect(result.current.isPlaying).toBe(true);
    act(() => result.current.beginScrub());
    expect(result.current.isPlaying).toBe(false);
    act(() => result.current.seek(750, false));
    expect(result.current.authoritativeCursorMs).toBe(500);
    act(() => result.current.reconcile());
    expect(result.current.authoritativeCursorMs).toBe(750);
  });

  it("supports explicit play/pause and pauses a hidden document", () => {
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    const { result } = renderHook(() => useReplayController(0, 1_000, 500));
    act(() => result.current.togglePlaying());
    expect(result.current.isPlaying).toBe(true);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(result.current.isPlaying).toBe(false);
    expect(result.current.authoritativeCursorMs).toBe(500);
    hidden.mockRestore();
  });

  it("stops playback exactly at the declared session end", () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal(
      "requestAnimationFrame",
      vi.fn((callback: FrameRequestCallback) => {
        frames.push(callback);
        return frames.length;
      }),
    );
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const { result } = renderHook(() => useReplayController(0, 1_000, 900));
    act(() => result.current.setPlaying(true));
    expect(frames).toHaveLength(1);
    act(() => frames[0]!(performance.now() + 1_000));
    expect(result.current.cursorMs).toBe(1_000);
    expect(result.current.authoritativeCursorMs).toBe(1_000);
    expect(result.current.isPlaying).toBe(false);
    vi.unstubAllGlobals();
  });
});
