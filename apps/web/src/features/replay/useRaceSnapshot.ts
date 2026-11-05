"use client";
import { getRaceState } from "@/lib/api/client";
import type { RaceState } from "@/lib/api/types";
import { useEffect, useRef, useState } from "react";

export function useRaceSnapshot(
  sessionId: string,
  timeMs: number,
  playing: boolean,
  refresh: number,
) {
  const target = useRef(timeMs);
  const anchor = playing ? "playing" : Math.round(timeMs);
  const key = `${sessionId}:${anchor}:${refresh}`;
  const [state, setState] = useState<{
    key: string;
    data: RaceState | null;
    error: string | null;
  }>({ key: "", data: null, error: null });
  useEffect(() => {
    target.current = timeMs;
  }, [timeMs]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      try {
        const data = await getRaceState(
          sessionId,
          { timeMs: Math.round(target.current) },
          { signal: controller.signal },
        );
        if (controller.signal.aborted) return;
        if (data.session_time_ms <= Math.round(target.current))
          setState({ key, data, error: null });
        if (playing) timer = setTimeout(() => void read(), 250);
      } catch (reason: unknown) {
        if (!controller.signal.aborted)
          setState({
            key,
            data: null,
            error:
              reason instanceof Error
                ? reason.message
                : "Race state could not be loaded.",
          });
      }
    }
    void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [sessionId, key, playing]);
  const data =
    state.key === key &&
    state.data &&
    state.data.session_time_ms <= Math.round(timeMs) &&
    (playing || state.data.session_time_ms === Math.round(timeMs))
      ? state.data
      : null;
  return { data, error: state.key === key ? state.error : null };
}
