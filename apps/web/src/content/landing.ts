export const heroContent = {
  eyebrow: "RACE REPLAY / PREDICTIVE INTELLIGENCE / STRATEGY SIMULATION",
  title: "DOWNFORCE",
  descriptor: "READ THE RACE.\nMODEL THE NEXT MOVE.",
  body: "A causal Formula 1 analysis platform for historical replay, machine-learning intelligence and probabilistic strategy simulation.",
  primaryCta: "ENTER WORKSPACE",
  secondaryCta: "EXPLORE THE SYSTEM",
  proof:
    "13-RACE ML V1 • 14,422 CANONICAL LAPS • 9,734 PACE / TYRE SAMPLES • MONTE CARLO STRATEGY ENGINE",
  microcopy:
    "Reconstruct what happened. Estimate what comes next. Test what could have changed.",
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

export const productStories = [
  {
    label: "01 / PLATFORM",
    title: "RECONSTRUCT THE RACE\nBEFORE YOU PREDICT IT.",
    body: "DOWNFORCE rebuilds race state at an exact historical moment — position, stint, tyre, pit, weather and race-control context — before any model is allowed to reason about what comes next.",
    bullets: ["CAUSAL STATE", "TEMPORAL REPLAY", "NO HINDSIGHT"],
  },
  {
    label: "02 / REPLAY",
    title: "SEE ONLY WHAT\nWAS KNOWABLE THEN.",
    body: "Move through a Grand Prix at any point in time and inspect the race as it existed at that cursor, not through information learned after the fact.",
    bullets: ["TIMING", "TRACK POSITION", "STINT CONTEXT"],
  },
  {
    label: "03 / INTELLIGENCE",
    title: "ESTIMATE THE NEXT LAP.\nKEEP THE UNCERTAINTY.",
    body: "Model representative pace, tyre residual behavior and effective pit loss while carrying calibrated uncertainty into every supported estimate.",
    bullets: ["PACE", "TYRES", "PIT LOSS"],
  },
  {
    label: "04 / SIMULATION",
    title: "RUN THOUSANDS OF\nPLAUSIBLE FUTURES.",
    body: "Candidate strategies move through a multi-driver Monte Carlo engine where pace, tyre and pit uncertainty follow every simulated path.",
    bullets: ["MONTE CARLO", "MULTI-DRIVER", "DISTRIBUTIONS"],
  },
  {
    label: "05 / STRATEGY",
    title: "COMPARE THE DECISION.\nNOT THE HINDSIGHT.",
    body: "Pit timing, compounds and alternatives are compared under shared stochastic conditions. If the evidence cannot separate them, DOWNFORCE returns no clear preference.",
    bullets: ["PIT WINDOWS", "ROBUSTNESS", "SENSITIVITY"],
  },
  {
    label: "06 / COUNTERFACTUALS",
    title: "CHANGE ONE CALL.\nREBUILD THE POSSIBILITY.",
    body: "Replace a historical decision at its original causal boundary and simulate from there. The observed result enters only afterward for comparison.",
    bullets: [
      "ALTERNATIVE STRATEGY",
      "CAUSAL BOUNDARY",
      "OBSERVED ≠ SIMULATED",
    ],
  },
  {
    label: "07 / ENGINEERING",
    title: "WHEN THE MODEL\nDOESN’T KNOW,\nIT SAYS SO.",
    body: "Explicit contracts, calibrated uncertainty and structured unavailable states keep unsupported assumptions from masquerading as precision.",
    bullets: ["FAIL CLOSED", "VERSIONED MODELS", "EXPLICIT LIMITS"],
  },
] as const;

export const finalCtaContent = {
  label: "ENTER DOWNFORCE",
  title: "REPLAY THE RACE.\nMODEL THE NEXT MOVE.",
  body: "Historical replay, predictive intelligence and probabilistic strategy analysis inside one engineering system.",
  primaryCta: "ENTER WORKSPACE",
  secondaryCta: "EXPLORE THE PLATFORM",
  note: "REPLAY / INTELLIGENCE / STRATEGY / COUNTERFACTUALS",
} as const;

export const footerContent = {
  primary:
    "© 2026 DOWNFORCE. Built as a motorsport analysis and simulation product experience.",
  secondary:
    "Formula 1, team names, and related marks remain the property of their respective owners. Final legal wording may be revised.",
} as const;
