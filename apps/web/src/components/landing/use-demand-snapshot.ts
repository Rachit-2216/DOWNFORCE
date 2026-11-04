import { useThree } from "@react-three/fiber";
import { useEffect } from "react";
import type { CarScene } from "./car-motion";

export function useDemandSnapshot(
  scene: CarScene,
  active: boolean,
  animate: boolean,
) {
  const invalidate = useThree((state) => state.invalidate);
  useEffect(() => {
    if (active && !animate) invalidate();
  }, [scene, active, animate, invalidate]);
}
