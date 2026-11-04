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
  finalCtaContent,
  footerContent,
  heroContent,
  publicFeatures,
} from "@/content/landing";
import { OnlineTools } from "@/components/product/OnlineTools";

import { CarAnatomy } from "./CarAnatomy";
import { type CarScene } from "./car-motion";
import { scrollChapter, selectionAtChapter } from "./scroll-choreography";

const RB22Scene = dynamic(() => import("./RB22Scene"), {
  ssr: false,
  loading: () => <StaticCar />,
});

function StaticCar() {
  return (
    <div className="machine-static" aria-hidden="true">
      <img
        className="machine-static__subject"
        src="/images/rb22-static.png"
        alt=""
      />
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
    return this.state.failed ? <StaticCar /> : this.props.children;
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
  const [interactive, setInteractive] = useState(false);
  const [webgl, setWebgl] = useState(false);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    const compact = window.matchMedia(
      "(max-width: 800px), (max-height: 759px)",
    );
    const supported = hasWebGL();
    const update = () => {
      setWebgl(supported);
      setInteractive(!reduced.matches && !compact.matches && supported);
    };
    update();
    reduced.addEventListener("change", update);
    compact.addEventListener("change", update);
    return () => {
      reduced.removeEventListener("change", update);
      compact.removeEventListener("change", update);
    };
  }, []);
  return { interactive, webgl };
}

export function LandingExperience() {
  const [anatomyScene, setAnatomyScene] = useState<CarScene>("reassemble");
  const [chapter, setChapter] = useState(() => scrollChapter(0));
  const [stageVisible, setStageVisible] = useState(true);
  const [documentVisible, setDocumentVisible] = useState(true);
  const [compactRequested, setCompactRequested] = useState(false);
  const machineRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const anatomyRef = useRef<HTMLDivElement>(null);
  const transferRef = useRef(0);
  const chapterRef = useRef("transfer");
  const { interactive, webgl } = usePresentationMode();
  const scene = chapter.phase === "transfer" ? "hero" : anatomyScene;

  useEffect(() => {
    const update = () => setDocumentVisible(!document.hidden);
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => {
    const machine = machineRef.current;
    const anatomy = anatomyRef.current;
    if (!machine || !anatomy || !interactive) return;
    let frame = 0;
    const update = () => {
      frame = 0;
      const rect = anatomy.getBoundingClientRect();
      const viewport = window.innerHeight;
      const transfer = Math.max(
        0,
        Math.min(1, 1 - rect.top / (viewport * 0.8)),
      );
      const progress =
        rect.top > 0
          ? transfer * 0.14
          : 0.14 + (-rect.top / Math.max(1, rect.height - viewport)) * 0.86;
      transferRef.current = transfer;
      machine.style.setProperty("--machine-transfer", String(transfer));
      machine.style.setProperty(
        "--tour-progress",
        String(Math.max(0, Math.min(1, progress))),
      );
      const next = scrollChapter(progress);
      // Per-pixel progress stays outside React. A native button selection is
      // respected until actual scrolling crosses into another chapter.
      if (next.key !== chapterRef.current) {
        const previousKey = chapterRef.current;
        chapterRef.current = next.key;
        setChapter(next);
        setAnatomyScene((current) =>
          selectionAtChapter(current, previousKey, next),
        );
      }
    };
    const requestUpdate = () => {
      if (!frame) frame = window.requestAnimationFrame(update);
    };
    update();
    window.addEventListener("scroll", requestUpdate, { passive: true });
    window.addEventListener("resize", requestUpdate);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("scroll", requestUpdate);
      window.removeEventListener("resize", requestUpdate);
    };
  }, [interactive]);

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const visibilityObserver = new IntersectionObserver(
      ([entry]) => setStageVisible(Boolean(entry?.isIntersecting)),
      { threshold: 0 },
    );
    if (stageRef.current) visibilityObserver.observe(stageRef.current);
    return () => {
      visibilityObserver.disconnect();
    };
  }, []);

  return (
    <div className="public-experience">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <header className="public-nav">
        <Link className="public-wordmark" href="/" aria-label="DOWNFORCE home">
          <i aria-hidden="true" />
          DOWNFORCE
        </Link>
        <nav aria-label="Main navigation">
          <Link href="/app">Explore</Link>
          <Link href="/app/analytics">Analytics</Link>
          <Link href="/app/replays">Replays</Link>
          <a href="#anatomy">Anatomy</a>
        </nav>
        <Link className="public-nav__entry" href="/app/replays">
          Open the app <span aria-hidden="true">↗</span>
        </Link>
      </header>
      <main id="main-content">
        <div
          className="public-machine"
          ref={machineRef}
          data-tour={interactive}
        >
          <div className="machine-track" aria-hidden="true">
            <div
              ref={stageRef}
              className={`machine-stage${stageVisible && documentVisible ? "" : " machine-stage--paused"}`}
            >
              {interactive ? (
                <SceneBoundary scene={scene}>
                  <RB22Scene
                    active={stageVisible && documentVisible}
                    scene={scene}
                    transfer={transferRef}
                  />
                </SceneBoundary>
              ) : (
                <StaticCar />
              )}
              <p className="machine-datum">
                RB22 / ENGINEERING STUDY <span>08 SYSTEMS</span>
              </p>
            </div>
          </div>
          <section
            className="public-hero"
            id="public-hero"
            aria-labelledby="public-title"
          >
            <div className="public-hero__copy">
              <p className="public-eyebrow">{heroContent.eyebrow}</p>
              <h1 id="public-title">{heroContent.title}</h1>
              <h2>{heroContent.descriptor}</h2>
              <p className="public-lead">{heroContent.body}</p>
              <div className="public-actions">
                <Link
                  className="public-button public-button--primary"
                  href="/app/replays"
                >
                  {heroContent.primaryCta} <span aria-hidden="true">↗</span>
                </Link>
                <Link className="public-button" href="/app">
                  {heroContent.secondaryCta} <span aria-hidden="true">→</span>
                </Link>
              </div>
              <p className="public-hero__note">
                No setup. Choose a race and start exploring.
              </p>
            </div>
            <div className="machine-mobile-hero">
              <StaticCar />
            </div>
            <div
              className="public-snapshot"
              aria-label="Current published coverage"
            >
              <span className="public-eyebrow">PUBLISHED SNAPSHOT</span>
              <span>27 seasons</span>
              <span>515 races</span>
              <span>13 detailed replays</span>
              <a href="#anatomy">
                Inside the machine <span aria-hidden="true">↓</span>
              </a>
            </div>
          </section>
          <div className="anatomy-container" ref={anatomyRef}>
            <CarAnatomy
              scene={anatomyScene}
              onSceneChange={(next) => {
                setAnatomyScene(next);
                setCompactRequested(true);
              }}
              tourPhase={interactive ? chapter.phase : undefined}
              stage={
                !interactive && webgl && compactRequested ? (
                  <SceneBoundary scene={anatomyScene}>
                    <RB22Scene
                      active={documentVisible}
                      scene={anatomyScene}
                      transfer={transferRef}
                      animate={false}
                    />
                  </SceneBoundary>
                ) : undefined
              }
            />
          </div>
        </div>
        <section
          className="public-products"
          aria-labelledby="products-title"
          id="online-tools"
        >
          <header className="public-section-heading">
            <p className="public-eyebrow">02 / FROM THE MACHINE TO THE RACE</p>
            <h2 id="products-title">
              <span className="public-heading-line">
                Understand what happened.
              </span>
              <br />
              <span className="public-heading-line">
                Question what comes next.
              </span>
            </h2>
            <p>
              One workspace. A clear boundary between the historical record,
              model estimates and simulated alternatives.
            </p>
          </header>
          <div
            className="public-evidence"
            aria-label="How to read the evidence"
          >
            <span>
              <i />
              Observed <small>The historical record</small>
            </span>
            <span>
              <i />
              Predicted <small>Estimates with uncertainty</small>
            </span>
            <span>
              <i />
              Simulated <small>Plausible alternatives</small>
            </span>
          </div>
          <OnlineTools />
          <div className="public-feature-grid">
            {publicFeatures.map((feature, index) => (
              <article
                className={`public-feature public-feature--${feature.kind}`}
                key={feature.label}
              >
                <div className="public-feature__top">
                  <span className="public-eyebrow">{feature.label}</span>
                  <span aria-hidden="true">0{index + 1}</span>
                </div>
                <h3>{feature.title}</h3>
                <p>{feature.body}</p>
                <ul>
                  {feature.details.map((detail) => (
                    <li key={detail}>{detail}</li>
                  ))}
                </ul>
                <Link href={feature.href}>
                  {feature.action} <span aria-hidden="true">↗</span>
                </Link>
              </article>
            ))}
          </div>
          <div className="public-trust">
            <span className="public-eyebrow">
              UNCERTAINTY IS PART OF THE ANSWER
            </span>
            <p>
              When the evidence cannot separate strategies, there is no clear
              preference. Unsupported estimates stay unavailable, not disguised
              as precision.
            </p>
          </div>
        </section>
        <section className="public-final" aria-labelledby="final-title">
          <p className="public-eyebrow">{finalCtaContent.label}</p>
          <h2 id="final-title">{finalCtaContent.title}</h2>
          <p>{finalCtaContent.body}</p>
          <div className="public-actions">
            <Link
              className="public-button public-button--primary"
              href="/app/replays"
            >
              {finalCtaContent.primaryCta}
              <span aria-hidden="true">↗</span>
            </Link>
            <Link className="public-button" href="/app/analytics">
              {finalCtaContent.secondaryCta}
              <span aria-hidden="true">→</span>
            </Link>
          </div>
        </section>
      </main>
      <footer className="public-footer">
        <Link className="public-wordmark" href="/">
          DOWNFORCE
        </Link>
        <div>
          <p>{footerContent.primary}</p>
          <p>{footerContent.secondary}</p>
          <div className="public-footer__links">
            <a href="https://github.com/Rachit-2216/DOWNFORCE">
              Source on GitHub ↗
            </a>
            <a href="https://github.com/Rachit-2216/DOWNFORCE#data-accuracy-and-attribution">
              Data attribution ↗
            </a>
          </div>
        </div>
        <a href="#public-hero">Back to top ↑</a>
      </footer>
    </div>
  );
}
