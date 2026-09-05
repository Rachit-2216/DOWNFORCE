"use client";

/* eslint-disable @next/next/no-img-element -- Canvas fallback must render without the Next image runtime */

import { Html, useGLTF } from "@react-three/drei";
import { Canvas, useFrame } from "@react-three/fiber";
import { Suspense, useMemo, useRef } from "react";
import {
  AmbientLight,
  Box3,
  Color,
  DirectionalLight,
  Euler,
  MathUtils,
  Mesh,
  MeshStandardMaterial,
  Object3D,
  PerspectiveCamera,
  PointLight,
  Quaternion,
  SpotLight,
  Vector3,
  type Material,
} from "three";

import {
  explosionOffsets,
  landingPoses,
  semanticGroups,
  type CarScene,
  type SemanticGroup,
} from "./car-motion";

const MODEL_URL = "/models/rb22-downforce-web.glb";
const HERO_TURN_SECONDS = 12;

type MaterialBaseline = {
  opacity: number;
  transparent: boolean;
  depthWrite: boolean;
  emissiveIntensity: number | null;
  emissive: Color | null;
};

const UNDERFLOOR_EMISSIVE = new Color("#0b2a59");

type TransformBaseline = {
  position: Vector3;
  quaternion: Quaternion;
  scale: Vector3;
  localCenter: Vector3;
};

type ModelState = {
  scene: Object3D;
  center: Vector3;
  groups: Map<SemanticGroup, Object3D>;
  transforms: Map<SemanticGroup, TransformBaseline>;
  materials: Map<SemanticGroup, Map<Material, MaterialBaseline>>;
};

function cloneMaterials(root: Object3D) {
  const cloned = new Map<Material, Material>();
  const baselines = new Map<Material, MaterialBaseline>();

  root.traverse((object) => {
    if (!(object instanceof Mesh)) return;
    const source: Material[] = Array.isArray(object.material)
      ? object.material
      : [object.material];
    const next = source.map((material) => {
      let local = cloned.get(material);
      if (!local) {
        const created = material.clone();
        local = created;
        cloned.set(material, created);
        const standard = created as MeshStandardMaterial;
        baselines.set(created, {
          opacity: created.opacity,
          transparent: created.transparent,
          depthWrite: created.depthWrite,
          emissiveIntensity:
            "emissiveIntensity" in standard ? standard.emissiveIntensity : null,
          emissive: "emissive" in standard ? standard.emissive.clone() : null,
        });
      }
      return local;
    });
    object.material = Array.isArray(object.material) ? next : next[0]!;
    object.castShadow = true;
    object.receiveShadow = true;
  });

  return baselines;
}

function useSemanticModel(): ModelState {
  const gltf = useGLTF(MODEL_URL, "/draco/");

  return useMemo(() => {
    const scene = gltf.scene.clone(true);
    const groups = new Map<SemanticGroup, Object3D>();
    const transforms = new Map<SemanticGroup, TransformBaseline>();
    const materials = new Map<SemanticGroup, Map<Material, MaterialBaseline>>();

    scene.updateMatrixWorld(true);
    const center = new Box3().setFromObject(scene).getCenter(new Vector3());
    semanticGroups.forEach((name) => {
      const group = scene.getObjectByName(name);
      if (!group) throw new Error(`RB22 semantic group missing: ${name}`);

      const worldCenter = new Box3()
        .setFromObject(group)
        .getCenter(new Vector3());
      const localCenter = group.worldToLocal(worldCenter.clone());
      groups.set(name, group);
      transforms.set(name, {
        position: group.position.clone(),
        quaternion: group.quaternion.clone(),
        scale: group.scale.clone(),
        localCenter,
      });
      materials.set(name, cloneMaterials(group));
    });

    // The source GLB is authored around an offset engineering datum. Rebase the
    // visual model once so rotations stay in-frame and every pose uses the same
    // predictable camera-space origin.
    scene.position.sub(center);

    return { scene, center, groups, transforms, materials };
  }, [gltf.scene]);
}

function SceneLighting({ scene }: { scene: CarScene }) {
  const ambient = useRef<AmbientLight>(null);
  const key = useRef<DirectionalLight>(null);
  const rim = useRef<DirectionalLight>(null);
  const underFill = useRef<DirectionalLight>(null);
  const underside = useRef<PointLight>(null);
  const redRim = useRef<SpotLight>(null);
  const warmGlint = useRef<PointLight>(null);

  useFrame((_, delta) => {
    const mode = landingPoses[scene].lighting;
    const isHero = scene === "hero" || scene === "hero-center";
    const isExplosion = scene === "exploded" || scene === "exploded-restore";
    const isSpotlight = scene.startsWith("spotlight-");
    const isStory = scene.startsWith("story-");
    const isCta = scene === "final-cta";
    const damping = 1 - Math.exp(-delta * 3.1);
    if (ambient.current) {
      const target =
        mode === "under"
          ? 2.25
          : isExplosion
            ? 0.95
            : isStory
              ? 0.64
              : isCta
                ? 0.82
                : isHero
                  ? 0.78
                  : 0.72;
      ambient.current.intensity = MathUtils.lerp(
        ambient.current.intensity,
        target,
        damping,
      );
    }
    if (key.current) {
      const target =
        mode === "front"
          ? 6.3
          : mode === "side"
            ? 5.5
            : mode === "under"
              ? 7.1
              : isCta
                ? 6.2
                : isStory
                  ? 5.65
                  : isExplosion
                    ? 5.5
                    : 5.35;
      key.current.intensity = MathUtils.lerp(
        key.current.intensity,
        target,
        damping,
      );
    }
    if (rim.current) {
      const target = isCta
        ? 4.8
        : mode === "side" || mode === "rear"
          ? 4.25
          : isStory
            ? 3.55
            : 2.85;
      rim.current.intensity = MathUtils.lerp(
        rim.current.intensity,
        target,
        damping,
      );
    }
    if (underside.current) {
      const target = mode === "under" ? 80 : 2.2;
      underside.current.intensity = MathUtils.lerp(
        underside.current.intensity,
        target,
        damping,
      );
    }
    if (underFill.current) {
      underFill.current.intensity = MathUtils.lerp(
        underFill.current.intensity,
        mode === "under" ? 7.5 : 0.35,
        damping,
      );
    }
    if (redRim.current) {
      const target = isCta
        ? 16
        : isStory
          ? 9
          : isSpotlight
            ? 7
            : isHero
              ? 6
              : 3.5;
      redRim.current.intensity = MathUtils.lerp(
        redRim.current.intensity,
        target,
        damping,
      );
    }
    if (warmGlint.current) {
      const target = isCta
        ? 4
        : scene === "story-strategy"
          ? 2.5
          : isHero
            ? 1.5
            : 0.6;
      warmGlint.current.intensity = MathUtils.lerp(
        warmGlint.current.intensity,
        target,
        damping,
      );
    }
  });

  return (
    <>
      <ambientLight ref={ambient} intensity={0.75} />
      <hemisphereLight color="#eaf2ff" groundColor="#020713" intensity={1.2} />
      <directionalLight
        ref={key}
        castShadow
        color="#dce8ff"
        intensity={4.8}
        position={[6, 9, 8]}
      />
      <directionalLight
        ref={rim}
        color="#1e41ff"
        intensity={3.1}
        position={[-8, 3, -3]}
      />
      <directionalLight
        ref={underFill}
        color="#718cff"
        intensity={0.35}
        position={[2, -8, 4]}
      />
      <pointLight
        ref={underside}
        color="#3157ff"
        intensity={2.2}
        position={[0, -4, 1]}
      />
      <spotLight
        ref={redRim}
        color="#f02d3e"
        intensity={6}
        angle={0.32}
        penumbra={0.85}
        position={[3, 0, -8]}
      />
      <pointLight
        ref={warmGlint}
        color="#f4d44d"
        intensity={1.5}
        distance={18}
        position={[-5, 4, 6]}
      />
    </>
  );
}

function RB22Model({ sceneState }: { sceneState: CarScene }) {
  const model = useSemanticModel();
  const rootRef = useRef<Object3D>(null);
  const cameraTarget = useRef(new Vector3());
  const scratchPosition = useRef(new Vector3());
  const scratchScale = useRef(new Vector3());
  const scratchCenter = useRef(new Vector3());
  const scratchRight = useRef(new Vector3());
  const scratchQuaternion = useRef(new Quaternion());

  useFrame(({ camera, clock, size }, delta) => {
    const root = rootRef.current;
    if (!root) return;

    const pose = landingPoses[sceneState];
    const damping = 1 - Math.exp(-delta * 3.25);
    const tabletFactor = size.width < 1060 ? 0.76 : 1;
    const rootX = pose.rootPosition[0] * tabletFactor;
    const rootScale = pose.rootScale * (size.width < 1060 ? 0.86 : 1);

    scratchPosition.current.set(
      rootX,
      pose.rootPosition[1],
      pose.rootPosition[2],
    );
    if (pose.horizontalOffset) {
      scratchRight.current.set(1, 0, 0).applyQuaternion(camera.quaternion);
      scratchPosition.current.addScaledVector(
        scratchRight.current,
        pose.horizontalOffset * tabletFactor,
      );
    }
    if (sceneState === "hero") {
      scratchPosition.current.y += Math.sin(clock.elapsedTime * 0.9) * 0.045;
    }
    root.position.lerp(scratchPosition.current, damping);
    root.scale.lerp(
      scratchScale.current.set(rootScale, rootScale, rootScale),
      damping,
    );

    const heroTurn =
      sceneState === "hero"
        ? (clock.elapsedTime * Math.PI * 2) / HERO_TURN_SECONDS
        : 0;
    scratchQuaternion.current.setFromEuler(
      new Euler(
        pose.rootRotation[0],
        pose.rootRotation[1] + heroTurn,
        pose.rootRotation[2],
      ),
    );
    root.quaternion.slerp(scratchQuaternion.current, damping);

    const cameraX =
      pose.cameraTarget[0] * tabletFactor +
      (pose.cameraPosition[0] - pose.cameraTarget[0]);
    camera.position.lerp(
      scratchPosition.current.set(
        cameraX,
        pose.cameraPosition[1],
        pose.cameraPosition[2],
      ),
      damping,
    );
    cameraTarget.current.lerp(
      scratchCenter.current.set(
        pose.cameraTarget[0] * tabletFactor,
        pose.cameraTarget[1],
        pose.cameraTarget[2],
      ),
      damping,
    );
    camera.lookAt(cameraTarget.current);
    const perspective = camera as PerspectiveCamera;
    perspective.fov = MathUtils.lerp(perspective.fov, pose.cameraFov, damping);
    perspective.updateProjectionMatrix();

    semanticGroups.forEach((name) => {
      const group = model.groups.get(name);
      const baseline = model.transforms.get(name);
      if (!group || !baseline) return;

      const isFocus = pose.focus?.group === name;
      const separated = pose.mode === "exploded" || pose.mode === "spotlight";
      const explosion = explosionOffsets[name];
      const targetPosition = scratchPosition.current
        .copy(baseline.position)
        .addScaledVector(
          scratchCenter.current.set(...explosion),
          separated ? (pose.mode === "spotlight" ? 1.22 : 1) : 0,
        );
      const targetQuaternion = scratchQuaternion.current.copy(
        baseline.quaternion,
      );
      const targetScale = scratchScale.current.copy(baseline.scale);

      if (isFocus && pose.focus) {
        targetQuaternion.multiply(
          new Quaternion().setFromEuler(new Euler(...pose.focus.rotation)),
        );
        targetScale.multiplyScalar(pose.focus.scale);
        const transformedCenter = baseline.localCenter
          .clone()
          .multiply(targetScale)
          .applyQuaternion(targetQuaternion);
        targetPosition
          .set(...pose.focus.center)
          .add(model.center)
          .sub(transformedCenter);
      }

      group.position.lerp(targetPosition, damping);
      group.quaternion.slerp(targetQuaternion, damping);
      group.scale.lerp(targetScale, damping);

      const targetOpacity = pose.mode !== "spotlight" || isFocus ? 1 : 0.035;
      model.materials.get(name)?.forEach((materialBaseline, material) => {
        material.opacity = MathUtils.lerp(
          material.opacity,
          materialBaseline.opacity * targetOpacity,
          damping,
        );
        material.transparent =
          materialBaseline.transparent || material.opacity < 0.995;
        material.depthWrite =
          targetOpacity > 0.8 ? materialBaseline.depthWrite : false;
        if (materialBaseline.emissiveIntensity !== null) {
          const standard = material as MeshStandardMaterial;
          const targetEmission =
            materialBaseline.emissiveIntensity +
            (isFocus ? (pose.lighting === "under" ? 0.9 : 0.08) : 0);
          standard.emissiveIntensity = MathUtils.lerp(
            standard.emissiveIntensity,
            targetEmission,
            damping,
          );
          if (materialBaseline.emissive) {
            standard.emissive.lerp(
              isFocus && pose.lighting === "under"
                ? UNDERFLOOR_EMISSIVE
                : materialBaseline.emissive,
              damping,
            );
          }
        }
      });
    });
  });

  const initial = landingPoses.hero;
  return (
    <group
      ref={rootRef}
      position={initial.rootPosition}
      rotation={initial.rootRotation}
      scale={initial.rootScale}
    >
      <primitive object={model.scene} />
    </group>
  );
}

function ModelLoader() {
  return (
    <Html center className="car-loader">
      <span />
      Loading RB22
    </Html>
  );
}

export default function RB22Scene({
  active,
  scene,
}: {
  active: boolean;
  scene: CarScene;
}) {
  return (
    <Canvas
      aria-hidden="true"
      camera={{ position: [10.2, 3.65, 10.8], fov: 32, near: 0.1, far: 120 }}
      dpr={[1, 1.5]}
      fallback={
        <img
          className="landing-car-static"
          src="/images/rb22-static.png"
          alt=""
        />
      }
      frameloop={active ? "always" : "never"}
      gl={{ alpha: true, antialias: true, powerPreference: "high-performance" }}
    >
      <SceneLighting scene={scene} />
      <Suspense fallback={<ModelLoader />}>
        <RB22Model sceneState={scene} />
      </Suspense>
    </Canvas>
  );
}
