"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { clamp } from "./replay-utils";

const STATE_RECONCILE_MS = 250;

export type ReplayControllerState = {
  cursorMs: number;
  authoritativeCursorMs: number;
  isPlaying: boolean;
  speed: number;
  seek: (milliseconds: number, reconcile?: boolean) => void;
  seekBy: (milliseconds: number) => void;
  setPlaying: (playing: boolean) => void;
  togglePlaying: () => void;
  setSpeed: (speed: number) => void;
  beginScrub: () => void;
  reconcile: () => void;
};

export function useReplayController(
  minimumMs: number,
  maximumMs: number,
  initialMs: number,
): ReplayControllerState {
  const [cursorMs, setCursorMs] = useState(() =>
    clamp(initialMs, minimumMs, maximumMs),
  );
  const [authoritativeCursorMs, setAuthoritativeCursorMs] = useState(() =>
    clamp(initialMs, minimumMs, maximumMs),
  );
  const [isPlaying, setPlayingState] = useState(false);
  const [speed, setSpeedState] = useState(1);
  const cursorRef = useRef(cursorMs);
  const playingRef = useRef(isPlaying);
  const speedRef = useRef(speed);

  useEffect(() => {
    cursorRef.current = cursorMs;
  }, [cursorMs]);
  useEffect(() => {
    playingRef.current = isPlaying;
  }, [isPlaying]);
  useEffect(() => {
    speedRef.current = speed;
  }, [speed]);

  const seek = useCallback(
    (milliseconds: number, reconcile = true) => {
      const next = clamp(milliseconds, minimumMs, maximumMs);
      cursorRef.current = next;
      setCursorMs(next);
      if (reconcile) setAuthoritativeCursorMs(next);
    },
    [maximumMs, minimumMs],
  );

  const reconcile = useCallback(
    () => setAuthoritativeCursorMs(cursorRef.current),
    [],
  );
  const setPlaying = useCallback(
    (playing: boolean) => setPlayingState(playing),
    [],
  );
  const togglePlaying = useCallback(
    () => setPlayingState((playing) => !playing),
    [],
  );
  const setSpeed = useCallback(
    (nextSpeed: number) => setSpeedState(clamp(nextSpeed, 0.5, 4)),
    [],
  );
  const seekBy = useCallback(
    (milliseconds: number) => seek(cursorRef.current + milliseconds),
    [seek],
  );
  const beginScrub = useCallback(() => {
    setPlayingState(false);
    setAuthoritativeCursorMs(cursorRef.current);
  }, []);

  useEffect(() => {
    if (!isPlaying) return;
    let frame = 0;
    let previous = performance.now();
    let lastReconcile = previous;

    const tick = (now: number) => {
      const elapsed = Math.min(250, now - previous);
      previous = now;
      const next = clamp(
        cursorRef.current + elapsed * speedRef.current,
        minimumMs,
        maximumMs,
      );
      cursorRef.current = next;
      setCursorMs(next);
      if (now - lastReconcile >= STATE_RECONCILE_MS || next >= maximumMs) {
        lastReconcile = now;
        setAuthoritativeCursorMs(next);
      }
      if (next >= maximumMs) {
        setPlayingState(false);
        return;
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [isPlaying, maximumMs, minimumMs]);

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.hidden && playingRef.current) {
        setPlayingState(false);
        setAuthoritativeCursorMs(cursorRef.current);
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () =>
      document.removeEventListener("visibilitychange", onVisibilityChange);
  }, []);

  return {
    cursorMs,
    authoritativeCursorMs,
    isPlaying,
    speed,
    seek,
    seekBy,
    setPlaying,
    togglePlaying,
    setSpeed,
    beginScrub,
    reconcile,
  };
}
