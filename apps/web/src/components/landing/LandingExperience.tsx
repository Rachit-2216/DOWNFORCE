"use client";

/* eslint-disable @next/next/no-img-element -- transparent source assets are also the WebGL fallback */

import dynamic from "next/dynamic";
import Link from "next/link";
import {
  Component,
  useEffect,
  useRef,
  useState,
  type ErrorInfo,
  type ReactNode,
} from "react";

import {
  componentSpotlights,
  finalCtaContent,
  footerContent,
  heroContent,
  productStories,
} from "@/content/landing";

import {
  landingPoses,
  sceneForComponent,
  sceneForStory,
  type CarScene,
} from "./car-motion";

const RB22Scene = dynamic(() => import("./RB22Scene"), {
  ssr: false,
  loading: () => <StaticCar scene="hero" label="Loading interactive RB22" />,
});

const componentImages: Partial<Record<CarScene, string>> = {
  "spotlight-front-wing": "/images/components/front_wing.png",
  "spotlight-nose": "/images/components/nose_front_structure.png",
  "spotlight-front-axle": "/images/components/front_axle_assembly.png",
  "spotlight-monocoque": "/images/components/monocoque_cockpit.png",
  "spotlight-sidepods": "/images/components/sidepods_engine_cover.png",
  "spotlight-floor": "/images/components/floor_diffuser.png",
  "spotlight-rear-axle": "/images/components/rear_axle_assembly.png",
  "spotlight-rear-wing": "/images/components/rear_wing.png",
};

function StaticCar({
  scene,
  label = "Red Bull RB22 race car",
}: {
  scene: CarScene;
  label?: string;
}) {
  const componentImage = componentImages[scene];
  return (
    <div
      className={`landing-car-fallback${componentImage ? " landing-car-fallback--component" : ""}`}
      role="img"
      aria-label={label}
    >
      <img src={componentImage ?? "/images/rb22-static.png"} alt="" />
    </div>
  );
}

class SceneBoundary extends Component<
  { children: ReactNode; scene: CarScene },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("RB22 WebGL scene unavailable", error, info.componentStack);
  }

  render() {
    return this.state.failed ? (
      <StaticCar scene={this.props.scene} />
    ) : (
      this.props.children
    );
  }
}

function hasWebGL() {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

function usePresentationMode() {
  const [mode, setMode] = useState<"interactive" | "static">("static");
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    const compact = window.matchMedia("(max-width: 720px)");
    const update = () =>
      setMode(
        reduced.matches || compact.matches || !hasWebGL()
          ? "static"
          : "interactive",
      );
    update();
    reduced.addEventListener("change", update);
    compact.addEventListener("change", update);
    return () => {
      reduced.removeEventListener("change", update);
      compact.removeEventListener("change", update);
    };
  }, []);
  return mode;
}

function navArea(scene: CarScene) {
  if (scene.startsWith("story-") || scene === "final-cta")
    return "capabilities";
  if (scene === "hero") return "hero";
  return "system";
}

export function LandingExperience() {
  const [scene, setScene] = useState<CarScene>("hero");
  const [documentVisible, setDocumentVisible] = useState(true);
  const narrativeRef = useRef<HTMLDivElement>(null);
  const presentationMode = usePresentationMode();
  const activeNav = navArea(scene);

  useEffect(() => {
    const update = () => setDocumentVisible(!document.hidden);
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => {
    const root = narrativeRef.current;
    if (!root || typeof IntersectionObserver === "undefined") return;
    const sections = [
      ...root.querySelectorAll<HTMLElement>("[data-car-scene]"),
    ];
    const observer = new IntersectionObserver(
      (entries) => {
        const active = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        const next = (active?.target as HTMLElement | undefined)?.dataset
          .carScene as CarScene | undefined;
        if (next) setScene(next);
      },
      { rootMargin: "-34% 0px -43%", threshold: [0, 0.12, 0.35, 0.65] },
    );
    sections.forEach((section) => observer.observe(section));
    return () => observer.disconnect();
  }, []);

  return (
    <div className="landing" ref={narrativeRef} data-active-scene={scene}>
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <header className="landing-nav">
        <Link className="wordmark wordmark--landing" href="/">
          <i aria-hidden="true" />
          DOWNFORCE
        </Link>
        <nav aria-label="Primary navigation">
          <a
            className={activeNav === "system" ? "is-active" : undefined}
            href="#system"
          >
            System
          </a>
          <a
            className={activeNav === "capabilities" ? "is-active" : undefined}
            href="#capabilities"
          >
            Capabilities
          </a>
          <Link className="nav-entry" href="/app">
            Open workspace <span aria-hidden="true">↗</span>
          </Link>
        </nav>
      </header>

      <div className="landing-car-layer" aria-live="off">
        <div className="landing-car-layer__frame">
          {presentationMode === "interactive" ? (
            <SceneBoundary scene={scene}>
              <RB22Scene active={documentVisible} scene={scene} />
            </SceneBoundary>
          ) : (
            <StaticCar scene={scene} />
          )}
        </div>
        <span className="landing-car-layer__datum">
          RB22 / SEMANTIC ASSEMBLY
        </span>
      </div>

      <main id="main-content">
        <section className="landing-hero" data-car-scene="hero">
          <div className="hero-copy">
            <p className="eyebrow">{heroContent.eyebrow}</p>
            <h1>{heroContent.title}</h1>
            <h2>{heroContent.descriptor}</h2>
            <p className="hero-copy__body">{heroContent.body}</p>
            <div className="landing-actions">
              <Link className="button button--primary" href="/app">
                {heroContent.primaryCta} <span aria-hidden="true">↗</span>
              </Link>
              <a className="button button--secondary" href="#system">
                {heroContent.secondaryCta} <span aria-hidden="true">↓</span>
              </a>
            </div>
            <p className="hero-copy__micro">{heroContent.microcopy}</p>
          </div>
          <div className="hero-proof" aria-label="Verified platform scope">
            {heroContent.proof.split(" • ").map((item) => (
              <span key={item}>{item}</span>
            ))}
          </div>
          <span className="scroll-cue" aria-hidden="true">
            Scroll to inspect <i />
          </span>
        </section>

        <section
          className="machine-handoff"
          data-car-scene="hero-center"
          aria-label="The machine takes center stage"
        >
          <p>THE MACHINE TAKES CENTER STAGE</p>
        </section>

        <section
          className="analysis-intro"
          id="system"
          data-car-scene="exploded"
          aria-labelledby="analysis-title"
        >
          <div>
            <p className="section-kicker">ANATOMY / PERFORMANCE SYSTEMS</p>
            <h2 id="analysis-title">
              EIGHT SYSTEMS.
              <br />
              ONE LAP TIME.
            </h2>
            <p>
              Separate the car. Follow the systems that turn airflow, grip and
              balance into performance.
            </p>
          </div>
        </section>

        <div className="component-sequence">
          {componentSpotlights.map((component, index) => {
            const componentScene = sceneForComponent(index);
            const textSide = landingPoses[componentScene].textSide;
            return (
              <section
                className={`component-spotlight component-spotlight--text-${textSide}`}
                data-car-scene={componentScene}
                aria-labelledby={`component-${index}-title`}
                key={component.group}
              >
                <article className="component-card">
                  <div className="component-card__progress">
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    <i>
                      <b
                        style={{
                          width: `${((index + 1) / componentSpotlights.length) * 100}%`,
                        }}
                      />
                    </i>
                    <span>
                      {String(componentSpotlights.length).padStart(2, "0")}
                    </span>
                  </div>
                  <p className="section-kicker">
                    {String(index + 1).padStart(2, "0")} — {component.system}
                  </p>
                  <h2 id={`component-${index}-title`}>{component.title}</h2>
                  <p>{component.body}</p>
                  <div className="technical-tags">
                    {component.tags.map((tag) => (
                      <span key={tag}>{tag}</span>
                    ))}
                  </div>
                </article>
              </section>
            );
          })}
        </div>

        <section
          className="exploded-restore"
          data-car-scene="exploded-restore"
          aria-label="All systems return to the exploded assembly"
        >
          <p>08 / 08 — ALL SYSTEMS IN VIEW</p>
        </section>

        <section className="reassembly" data-car-scene="reassemble">
          <div>
            <p className="section-kicker">SYSTEM STATE / ASSEMBLED</p>
            <h2>THE MACHINE IS ONE SYSTEM. SO IS THE RACE.</h2>
          </div>
        </section>

        <div className="product-story" id="capabilities">
          {productStories.map((story, index) => {
            const storyScene = sceneForStory(index);
            const textSide = landingPoses[storyScene].textSide;
            return (
              <section
                className={`story-section story-section--text-${textSide}`}
                data-car-scene={storyScene}
                aria-labelledby={`story-${index}-title`}
                key={story.label}
              >
                <article className="story-card">
                  <p className="section-kicker">{story.label}</p>
                  <h2 id={`story-${index}-title`}>{story.title}</h2>
                  <p>{story.body}</p>
                  <ul>
                    {story.bullets.map((bullet) => (
                      <li key={bullet}>{bullet}</li>
                    ))}
                  </ul>
                </article>
              </section>
            );
          })}
        </div>

        <section className="landing-cta" data-car-scene="final-cta">
          <div className="landing-cta__content">
            <span className="landing-cta__index" aria-hidden="true">
              08 / ENTER
            </span>
            <div className="landing-cta__copy">
              <p className="section-kicker">{finalCtaContent.label}</p>
              <h2>{finalCtaContent.title}</h2>
              <p>{finalCtaContent.body}</p>
              <div className="landing-actions">
                <Link className="button button--primary" href="/app">
                  {finalCtaContent.primaryCta} <span aria-hidden="true">↗</span>
                </Link>
                <a className="button button--secondary" href="#system">
                  {finalCtaContent.secondaryCta}{" "}
                  <span aria-hidden="true">↑</span>
                </a>
              </div>
              <p className="landing-cta__note">{finalCtaContent.note}</p>
            </div>
          </div>
        </section>
      </main>

      <footer className="landing-footer">
        <Link className="wordmark wordmark--landing" href="/">
          DOWNFORCE
        </Link>
        <div>
          <p>{footerContent.primary}</p>
          <p>{footerContent.secondary}</p>
        </div>
        <Link href="/app">Workspace ↗</Link>
      </footer>
    </div>
  );
}
