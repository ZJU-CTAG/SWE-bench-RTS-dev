# Regression testing with NameRTS

NameRTS is an optional regression-test selection extension for local Docker
evaluation. Without `--namerts-artifact-root`, evaluation behaves exactly as the
standard SWE-bench harness.

## Initialize the submodule

The NameRTS implementation is kept in a separate repository:

```bash
git submodule update --init --recursive
```

NameRTS must use the same Python minor version as the instance test environment.
The preparation command chooses the matching `NameRTS36` through `NameRTS313`
environment supplied by the submodule.

## Prepare an instance once

Preparation derives a separately named `sweb.namerts.*` image from the official
instance image. It runs the full suite at the image's base/setup parent while
collecting dynamic dependencies, then runs the full suite once with the golden
patch and the official test setup to record failures already present in the
official environment. Dynamic dependency collection runs one test file per
instrumented process because NameRTS must attribute dependencies to a test
file. The non-instrumented golden baseline also runs one test file per process,
using the official SWE-bench runner prefix with bounded outer concurrency. The
default per-file timeout is 120 seconds.

```bash
python -m swebench.harness.namerts.prepare \
  --dataset_name SWE-bench/SWE-bench_Verified \
  --split test \
  --instance_ids astropy__astropy-12907 \
  --artifact_root namerts_artifacts \
  --workers 8
```

Each `<artifact-root>/<instance-id>/` contains `manifest.json`,
`parent_cache.tar.gz`, `golden_baseline.json`, and preparation logs. Artifacts
bind the instance, patches, eval script/parser, official and derived image IDs,
Python version, and NameRTS source fingerprint. Use `--force_rebuild true` only
when intentionally replacing that instance's artifact and derived image.
Before creating an inspection or preparation container, the preparer removes an
interrupted container for the same instance and stage. After a runtime image is
successfully built or reused, it also removes older fingerprint tags for that
same instance without force-removing images used by external containers. This
keeps recipe/source upgrades from accumulating duplicate `sweb.namerts.*`
images while preserving the exact image referenced by the new artifact.
Grouped-runner artifacts use schema version 2 and are intentionally rejected;
prepare them again to obtain a per-file schema version 5 artifact. Parsed
failure identities normalize ephemeral `/tmp` paths and memory addresses before
golden/candidate comparison.

Python 3.5 instances `django__django-10097` and `django__django-7530`, and the
Python 2.7 instance `psf__requests-1724`, use ordinary evaluation because those
runtimes are not supported by NameRTS.

## Evaluate predictions

```bash
swebench eval verified \
  --predictions preds.json \
  --run-id model-rts \
  --workers 8 \
  --namerts-artifact-root namerts_artifacts \
  --namerts-workers 8 \
  --namerts-per-file-timeout 120 \
  --namerts-timeout 7200
```

The harness first runs and grades the normal SWE-bench tests in the official
image. An upstream-unresolved patch does not load an artifact or start an RTS
container. For an upstream-resolved patch, the harness starts a derived runtime
container and selects affected tests from the prepared parent cache. It reuses
the official image's `/testbed` editable installation, or reruns the official
install command for a non-editable project, then restores the original PR's test
files to the dataset base, applies the dataset test patch, and runs the selected
tests one file per process with the same official runner prefix used during the
golden baseline. `--namerts-workers` bounds both instrumented collection and
per-file regression-test concurrency.

Files under GIS, fixture, or example directories are excluded by NameRTS's test
discovery because those trees commonly require optional services or are not
normally collected. If a remaining file fails collection or reaches its
timeout under the golden patch, it is recorded in the artifact and removed from
candidate selections because its golden result is incomplete. A new
candidate-side collection failure for a file that collected under the golden
patch is a regression. Golden runs also record each completed file's duration;
such a file receives a candidate timeout of at least three times its golden
duration plus 60 seconds (and never less than the preparation timeout), avoiding
cutoff-boundary differences under concurrent load. Per-file processes use
private temporary roots and Matplotlib config directories so independently run
test files cannot race in shared pytest or font/config caches. They also use a
fixed Python hash seed, and the legacy SymPy runner receives a fixed test seed,
so golden and candidate executions are comparable.

The final decision is:

```text
new_failures = selected_candidate_failures - golden_full_suite_failures
resolved = upstream_resolved and namerts_completed and not new_failures
```

Detailed selection, test, and error information is stored in the instance
report under `namerts_regression`. Missing or mismatched artifacts, image ID
mismatches, selection failures, and overall timeouts conservatively produce
`resolved: false`. A timeout is a terminal result for that run ID and is not
automatically retried. Existing ordinary reports can be augmented in place:
only their upstream-resolved instances start RTS containers.
