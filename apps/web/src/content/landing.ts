export const heroContent = {
  eyebrow: "FORMULA 1 / RACE INTELLIGENCE",
  title: "DOWNFORCE",
  descriptor: "Read the race.\nRethink the next move.",
  body: "Explore Formula 1 history, replay the race as it unfolded, and compare strategy with the evidence available at the time.",
  primaryCta: "Try a race",
  secondaryCta: "Explore history",
} as const;

export const componentSpotlights = [
  {
    group: "DF_FRONT_WING",
    system: "AERODYNAMIC SYSTEM",
    title: "Front Wing",
    body: "The first major aerodynamic control surface shapes airflow onset, front-end balance and the car’s initial response to direction change.",
    tags: ["FRONT BALANCE", "AIRFLOW CONDITIONING", "DOWNFORCE"],
  },
  {
    group: "DF_NOSE",
    system: "STRUCTURAL AERO",
    title: "Nose",
    body: "The nose connects impact structure and aerodynamic intent, guiding flow rearward while anchoring the car’s front architecture.",
    tags: ["IMPACT STRUCTURE", "FLOW DIRECTION", "PACKAGING"],
  },
  {
    group: "DF_FRONT_AXLE",
    system: "MECHANICAL PLATFORM",
    title: "Front Axle",
    body: "Tyre contact, suspension and steering geometry combine here to define entry stability, turn-in response and mechanical grip.",
    tags: ["TURN-IN", "SUSPENSION", "MECHANICAL GRIP"],
  },
  {
    group: "DF_MONOCOQUE",
    system: "SURVIVAL STRUCTURE",
    title: "Monocoque",
    body: "Cockpit, halo and survival cell form the architectural core around which every major assembly is organized.",
    tags: ["SURVIVAL CELL", "COCKPIT", "CORE STRUCTURE"],
  },
  {
    group: "DF_SIDEPODS_ENGINE",
    system: "THERMAL + BODYWORK",
    title: "Sidepods & Engine Cover",
    body: "The car’s midsection resolves cooling, internal packaging and airflow conditioning while feeding the rear aerodynamic package.",
    tags: ["COOLING", "PACKAGING", "FLOW CONDITIONING"],
  },
  {
    group: "DF_FLOOR_DIFFUSER",
    system: "GROUND EFFECT",
    title: "Floor & Diffuser",
    body: "The hidden aerodynamic surface accelerates underfloor flow and expands it through the diffuser to produce efficient downforce.",
    tags: ["VENTURI FLOW", "GROUND EFFECT", "EFFICIENCY"],
  },
  {
    group: "DF_REAR_AXLE",
    system: "TRACTION PLATFORM",
    title: "Rear Axle",
    body: "Rear tyres, suspension and drivetrain loads meet here, governing traction and the conversion of power into usable stint pace.",
    tags: ["TRACTION", "REAR GRIP", "POWER DELIVERY"],
  },
  {
    group: "DF_REAR_WING",
    system: "AERODYNAMIC SYSTEM",
    title: "Rear Wing",
    body: "The final control surface balances rear stability and downforce against straight-line drag across each circuit configuration.",
    tags: ["REAR BALANCE", "DRAG TRADE-OFF", "STABILITY"],
  },
] as const;

export const finalCtaContent = {
  label: "03 / YOUR NEXT MOVE",
  title: "The race is history.\nThe questions are yours.",
  body: "Start with a replay, or find the patterns across seasons. The workspace is ready in your browser.",
  primaryCta: "Choose a replay",
  secondaryCta: "Explore the analytics",
  note: "REPLAY / INTELLIGENCE / STRATEGY / COUNTERFACTUALS",
} as const;

export const footerContent = {
  primary: "© 2026 DOWNFORCE. An independent motorsport analysis project.",
  secondary:
    "Unofficial and not affiliated with Formula 1, the FIA or any team. Names, marks and car imagery belong to their respective owners.",
} as const;

export const publicFeatures = [
  {
    kind: "archive",
    label: "RACE ARCHIVE",
    title: "A season is more than its final standings.",
    body: "Explore the published race archive, from a season overview to the drivers and results of an individual Grand Prix.",
    details: ["Seasons and race results", "Driver and constructor context"],
    href: "/app",
    action: "Browse the archive",
  },
  {
    kind: "analytics",
    label: "ANALYTICS",
    title: "Find the pattern behind the result.",
    body: "Move beyond a single race. Compare the available season, driver and constructor records with the source and coverage in view.",
    details: ["Season comparisons", "Published historical coverage"],
    href: "/app/analytics",
    action: "Open analytics",
  },
  {
    kind: "replay",
    label: "HISTORICAL REPLAY / OBSERVED",
    title: "See only what was knowable then.",
    body: "Move the race cursor and inspect timing, position, tyres and stint context without borrowing information from the future.",
    details: ["Lap-by-lap race state", "Timing, tyres and pit context"],
    href: "/app/replays",
    action: "Find a race to replay",
  },
  {
    kind: "strategy",
    label: "INTELLIGENCE + STRATEGY",
    title: "Compare the decision. Not the hindsight.",
    body: "In a supported race context, inspect model estimates and test pit timing and compound alternatives. Keep observed, predicted and simulated evidence separate.",
    details: [
      "Pace, tyre and pit-loss estimates",
      "Strategy distributions, not guarantees",
    ],
    href: "/app/replays",
    action: "Explore a race context",
  },
] as const;
