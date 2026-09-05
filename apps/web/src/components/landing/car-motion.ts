export const semanticGroups = [
  "DF_FRONT_WING",
  "DF_NOSE",
  "DF_FRONT_AXLE",
  "DF_MONOCOQUE",
  "DF_SIDEPODS_ENGINE",
  "DF_FLOOR_DIFFUSER",
  "DF_REAR_AXLE",
  "DF_REAR_WING",
] as const;

export type SemanticGroup = (typeof semanticGroups)[number];
export type Vec3 = readonly [number, number, number];
export type TextSide = "left" | "right" | "center";

export type CarScene =
  | "hero"
  | "hero-center"
  | "exploded"
  | "spotlight-front-wing"
  | "spotlight-nose"
  | "spotlight-front-axle"
  | "spotlight-monocoque"
  | "spotlight-sidepods"
  | "spotlight-floor"
  | "spotlight-rear-axle"
  | "spotlight-rear-wing"
  | "exploded-restore"
  | "reassemble"
  | "story-platform"
  | "story-replay"
  | "story-intelligence"
  | "story-simulation"
  | "story-strategy"
  | "story-counterfactual"
  | "story-trust"
  | "final-cta";

type LightingMode = "studio" | "front" | "side" | "under" | "rear";
type PoseMode = "assembled" | "exploded" | "spotlight";

export type LandingPose = {
  mode: PoseMode;
  rootPosition: Vec3;
  rootRotation: Vec3;
  rootScale: number;
  cameraPosition: Vec3;
  cameraTarget: Vec3;
  cameraFov: number;
  horizontalOffset?: number;
  textSide: TextSide;
  lighting: LightingMode;
  focus?: {
    group: SemanticGroup;
    center: Vec3;
    scale: number;
    rotation: Vec3;
  };
};

export const explosionOffsets: Record<SemanticGroup, Vec3> = {
  DF_FRONT_WING: [-2.6, -1.05, 3.15],
  DF_NOSE: [0.15, -1.05, 1.75],
  DF_FRONT_AXLE: [-2.75, 0.65, 0.85],
  DF_MONOCOQUE: [-0.15, 1.05, 0.05],
  DF_SIDEPODS_ENGINE: [2.7, 0.15, -0.15],
  DF_FLOOR_DIFFUSER: [0.1, -2.45, -0.2],
  DF_REAR_AXLE: [2.65, 0.85, -1.25],
  DF_REAR_WING: [0.3, 2.6, -2.8],
};

const spotlight = (
  group: SemanticGroup,
  textSide: Exclude<TextSide, "center">,
  rootRotation: Vec3,
  scale: number,
  componentRotation: Vec3,
  lighting: LightingMode,
): LandingPose => {
  const visualCenter = textSide === "left" ? 2.55 : -2.55;
  return {
    mode: "spotlight",
    rootPosition: [visualCenter, 0, 0],
    rootRotation,
    rootScale: 1.08,
    cameraPosition: [0, 3.3, 11.4],
    cameraTarget: [0, 0, 0],
    cameraFov: 31,
    textSide,
    lighting,
    focus: {
      group,
      center: [0, 0, 0],
      scale,
      rotation: componentRotation,
    },
  };
};

const story = (
  textSide: Exclude<TextSide, "center">,
  rotationY: number,
  scale = 1.25,
): LandingPose => {
  const carCenter = textSide === "left" ? 2.8 : -2.8;
  return {
    mode: "assembled",
    rootPosition: [carCenter, -0.05, 0],
    rootRotation: [0.035, rotationY, -0.015],
    rootScale: scale,
    cameraPosition: [0, 3.25, 9.5],
    cameraTarget: [0, -0.1, 0],
    cameraFov: 33,
    textSide,
    lighting: "studio",
  };
};

export const landingPoses: Record<CarScene, LandingPose> = {
  hero: {
    mode: "assembled",
    rootPosition: [3, -0.1, 0],
    rootRotation: [0.04, -0.62, -0.018],
    rootScale: 1.2,
    cameraPosition: [0, 3.3, 9.6],
    cameraTarget: [0, -0.05, 0],
    cameraFov: 32,
    textSide: "left",
    lighting: "studio",
  },
  "hero-center": {
    mode: "assembled",
    rootPosition: [0, 0, 0],
    rootRotation: [-0.08, -0.48, -0.015],
    rootScale: 1.3,
    cameraPosition: [6.2, 4.1, 9.4],
    cameraTarget: [0, 0, 0],
    cameraFov: 34,
    textSide: "center",
    lighting: "studio",
  },
  exploded: {
    mode: "exploded",
    rootPosition: [0, 0, 0],
    rootRotation: [-0.13, -0.44, 0],
    rootScale: 1,
    cameraPosition: [6.5, 4.8, 10.5],
    cameraTarget: [0, 0, 0],
    cameraFov: 36,
    textSide: "center",
    lighting: "studio",
  },
  "spotlight-front-wing": spotlight(
    "DF_FRONT_WING",
    "right",
    [0.22, -0.05, 0],
    2.2,
    [0.1, 0, 0],
    "front",
  ),
  "spotlight-nose": spotlight(
    "DF_NOSE",
    "left",
    [0.28, -0.55, -0.02],
    2.15,
    [0, -0.08, 0],
    "front",
  ),
  "spotlight-front-axle": spotlight(
    "DF_FRONT_AXLE",
    "right",
    [0.2, -0.35, 0],
    2,
    [0.02, 0.12, 0],
    "front",
  ),
  "spotlight-monocoque": spotlight(
    "DF_MONOCOQUE",
    "left",
    [0.48, -0.72, 0.02],
    1.75,
    [0.08, 0.05, 0],
    "studio",
  ),
  "spotlight-sidepods": spotlight(
    "DF_SIDEPODS_ENGINE",
    "right",
    [0.06, -1.35, -0.02],
    1.62,
    [0, 0.08, 0],
    "side",
  ),
  "spotlight-floor": spotlight(
    "DF_FLOOR_DIFFUSER",
    "left",
    [-0.72, -0.55, 0.04],
    1.75,
    [-0.2, 0, 0.03],
    "under",
  ),
  "spotlight-rear-axle": spotlight(
    "DF_REAR_AXLE",
    "right",
    [0.22, 2.35, 0],
    2.1,
    [0.02, 0.08, 0],
    "rear",
  ),
  "spotlight-rear-wing": spotlight(
    "DF_REAR_WING",
    "left",
    [0.16, 3.05, 0],
    2.8,
    [0.08, 0, 0],
    "rear",
  ),
  "exploded-restore": {
    mode: "exploded",
    rootPosition: [0, 0, 0],
    rootRotation: [-0.13, -0.44, 0],
    rootScale: 1,
    cameraPosition: [6.5, 4.8, 10.5],
    cameraTarget: [0, 0, 0],
    cameraFov: 36,
    textSide: "center",
    lighting: "studio",
  },
  reassemble: {
    mode: "assembled",
    rootPosition: [0, 0.1, 0],
    rootRotation: [0.03, -0.72, -0.015],
    rootScale: 1.5,
    cameraPosition: [5.6, 3.2, 8.6],
    cameraTarget: [0, 0, 0],
    cameraFov: 32,
    textSide: "center",
    lighting: "studio",
  },
  "story-platform": story("left", -0.62, 1.15),
  "story-replay": story("right", 0.94, 1.1),
  "story-intelligence": story("left", 2.48, 1.15),
  "story-simulation": story("right", 4.02, 1.1),
  "story-strategy": story("left", 5.55, 1.15),
  "story-counterfactual": story("right", 7.08, 1.1),
  "story-trust": story("left", 8.62, 1.15),
  "final-cta": {
    mode: "assembled",
    rootPosition: [-2.4, -0.05, 0],
    rootRotation: [0.035, 8.75, -0.018],
    rootScale: 0.95,
    cameraPosition: [0, 3.2, 9.2],
    cameraTarget: [0, -0.05, 0],
    cameraFov: 32,
    textSide: "right",
    lighting: "side",
  },
};

export function sceneForComponent(index: number): CarScene {
  return [
    "spotlight-front-wing",
    "spotlight-nose",
    "spotlight-front-axle",
    "spotlight-monocoque",
    "spotlight-sidepods",
    "spotlight-floor",
    "spotlight-rear-axle",
    "spotlight-rear-wing",
  ][index] as CarScene;
}

export function sceneForStory(index: number): CarScene {
  return [
    "story-platform",
    "story-replay",
    "story-intelligence",
    "story-simulation",
    "story-strategy",
    "story-counterfactual",
    "story-trust",
  ][index] as CarScene;
}
