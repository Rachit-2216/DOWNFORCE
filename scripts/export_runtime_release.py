"""Validate and prepare a local product-only release; never upload or deploy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from downforce_core.runtime_blob.bundle import write_bundle
from downforce_core.runtime_blob.export import prepare_runtime_release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--profile", choices=("full", "visitor"), default="full")
    parser.add_argument(
        "--bundle-output",
        type=Path,
        help="Explicitly copy authenticated product objects into a new offline bundle directory",
    )
    parser.add_argument(
        "--write-metadata",
        action="store_true",
        help="Write only verified generated metadata to .downforce/deployment/releases/<sha>",
    )
    arguments = parser.parse_args()
    release = prepare_runtime_release(
        arguments.project_root, include_detailed=arguments.profile == "full"
    )
    output = release.write_metadata() if arguments.write_metadata else None
    bundle = write_bundle(release, arguments.bundle_output) if arguments.bundle_output else None
    print(
        json.dumps(
            {
                "profile": arguments.profile,
                "release_sha256": release.sha256,
                "file_count": len(release.manifest.files),
                "index_bytes": len(release.body),
                "payload_bytes": sum(entry.size_bytes for entry in release.manifest.files.values()),
                "max_object_bytes": max(
                    entry.size_bytes for entry in release.manifest.files.values()
                ),
                "metadata_directory": str(output) if output else None,
                "bundle_directory": str(bundle) if bundle else None,
                "source_payloads_copied": bundle is not None,
                "uploaded": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
