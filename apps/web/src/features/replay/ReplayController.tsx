import { useRef, type ChangeEvent } from "react";

import { formatClock } from "./replay-utils";
import type { ReplayControllerState } from "./useReplayController";

const SPEEDS = [0.5, 1, 2, 4] as const;

type Props = {
  controller: ReplayControllerState;
  minimumMs: number;
  maximumMs: number;
  referenceLap: number | null;
  maximumLap: number;
  trackStatus: string;
  onLapSeek: (lap: number) => void;
  isStateLoading: boolean;
};

export function ReplayController({
  controller,
  minimumMs,
  maximumMs,
  referenceLap,
  maximumLap,
  trackStatus,
  onLapSeek,
  isStateLoading,
}: Props) {
  const pointerScrubbing = useRef(false);
  const progress =
    ((controller.cursorMs - minimumMs) / Math.max(1, maximumMs - minimumMs)) *
    100;
  const handleChange = (event: ChangeEvent<HTMLInputElement>) =>
    controller.seek(Number(event.target.value), !pointerScrubbing.current);
  const handlePointerDown = () => {
    pointerScrubbing.current = true;
    controller.beginScrub();
  };
  const handleRelease = () => {
    pointerScrubbing.current = false;
    controller.reconcile();
  };
  const currentLap = referenceLap ?? 1;

  return (
    <section className="replay-controller" aria-label="Replay controls">
      <div className="replay-controller__transport">
        <button
          type="button"
          className="icon-button"
          onClick={() => onLapSeek(Math.max(1, currentLap - 1))}
          disabled={currentLap <= 1}
          aria-label="Previous leader lap"
        >
          ←<span>Lap</span>
        </button>
        <button
          type="button"
          className="play-button"
          onClick={controller.togglePlaying}
          aria-label={controller.isPlaying ? "Pause replay" : "Play replay"}
        >
          <span aria-hidden="true">{controller.isPlaying ? "Ⅱ" : "▶"}</span>
          {controller.isPlaying ? "Pause" : "Play"}
        </button>
        <button
          type="button"
          className="icon-button"
          onClick={() => onLapSeek(Math.min(maximumLap, currentLap + 1))}
          disabled={currentLap >= maximumLap}
          aria-label="Next leader lap"
        >
          <span>Lap</span>→
        </button>
      </div>
      <div className="replay-controller__timeline">
        <div className="replay-controller__readout">
          <strong>{formatClock(controller.cursorMs)}</strong>
          <span>
            Lap {referenceLap ?? "—"} / {maximumLap || "—"}
          </span>
          <span
            className="track-status"
            aria-label={`Track status ${trackStatus}`}
          >
            Track {trackStatus}
          </span>
          <i
            className={
              isStateLoading
                ? "sync-indicator sync-indicator--active"
                : "sync-indicator"
            }
          >
            {isStateLoading ? "Reconciling" : "Canonical state"}
          </i>
        </div>
        <input
          type="range"
          min={minimumMs}
          max={maximumMs}
          step={100}
          value={Math.round(controller.cursorMs)}
          onPointerDown={handlePointerDown}
          onPointerUp={handleRelease}
          onPointerCancel={handleRelease}
          onBlur={handleRelease}
          onChange={handleChange}
          aria-label="Replay time"
          style={{ "--replay-progress": `${progress}%` } as React.CSSProperties}
        />
        <div className="replay-controller__bounds">
          <span>{formatClock(minimumMs)}</span>
          <span>{formatClock(maximumMs)}</span>
        </div>
      </div>
      <div className="speed-control" aria-label="Playback speed">
        {SPEEDS.map((speed) => (
          <button
            type="button"
            aria-pressed={controller.speed === speed}
            onClick={() => controller.setSpeed(speed)}
            key={speed}
          >
            {speed}×
          </button>
        ))}
      </div>
    </section>
  );
}
