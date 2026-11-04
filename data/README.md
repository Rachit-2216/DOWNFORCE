# DOWNFORCE data workspace

This directory contains policy and small metadata only. Raw provider responses, caches, bulk
native telemetry, features and generated simulation outputs must not be committed to Git.

The narrow exception is `product-data/` at the repository root: a reviewed, immutable public
snapshot of normalized product records and model artifacts, with a pinned manifest and checksums.
It supports the read-only hosted edition without an external storage account. It is not a provider
cache, raw-response archive or the full local corpus. Attribution is in `DATA-NOTICE.txt`.

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
