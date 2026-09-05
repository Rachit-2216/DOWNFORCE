# DOWNFORCE data workspace

This directory contains policy and small metadata only. Historical timing, telemetry, provider
responses, normalized tables, features and simulation outputs must not be committed to Git.

Runtime data belongs under the ignored `.downforce/` tree:

```text
.downforce/
├── cache/        # Provider/library cache; disposable and reproducible
├── raw/          # Immutable source responses with provenance
├── normalized/   # Provider-neutral canonical records
├── processed/    # Reproducible features and model-ready material
└── simulations/  # Generated scenario outputs
```

Rules:

- Raw data is immutable. Corrections produce a new version with retrieval metadata.
- Normalized and processed data records the source version and transformation version.
- Cache entries may be deleted; raw snapshots may not be silently mutated.
- Secrets, access tokens and paid-provider responses never enter Git.
- Large local assets and datasets should use external storage; Git LFS is reserved for reviewed,
  version-worthy binaries, not caches or telemetry dumps.
