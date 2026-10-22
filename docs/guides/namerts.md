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
instance image. At its base/setup parent, instrumentation runs one test file
at a time and records each file's elapsed wall time. Each instrumentation file
has a **180-second hard timeout**, which terminates its process group. Timed-out
files are always excluded from both the golden regression baseline and selected
regression tests: their incomplete dependency capture is not trusted for coverage.
Completed files taking at least `--slow_test_threshold` (default 180 seconds)
are also excluded. This does not exclude any upstream F2P/P2P tests.

After applying the golden patch and official test setup, preparation runs the
discovered regression suite minus those slow files in **one official runner
invocation**. There is no added within-instance test multiprocessing or tox
isolation. The golden runner's whole-command deadline is `--total_timeout`
(alias `--timeout`, default 1800 seconds), independent of instrumentation.
The example uses 3600 seconds. This is not a shared end-to-end deadline:
instrumentation runs files serially with a separate 180-second limit per file.

### Persistent instance policy

`swebench/harness/namerts/instance_policy.json` is version-controlled and stores
each instance's `slow_test_files` and optional `pytest_version` / `py_version`
pins. Preparation skips known slow files before launching instrumentation,
including on a fresh image/artifact build. Newly observed slow or timed-out
files are atomically merged into this JSON before the golden runner starts,
so they survive a subsequent golden timeout. Concurrent workers use a file lock.
Review and commit the JSON changes to share observations; do not commit the lock.
The checkout must be writable to record new observations.

Both golden RTS baselines and selected regression tests exclude these files;
the original SWE-bench F2P/P2P script remains unchanged. Exclusions are deliberate
coverage gaps and persist until manually removed from the JSON. Artifacts bind
the policy snapshot; changed dependency pins or newly excluded files require
fresh preparation. Pins affect the derived testbed only, never official images.
Image fingerprints include dependency pins; slow-list growth alone does not
require rebuilding images. The built-in list is seeded from the previous
13-instance SymPy experiment (files reaching 180 seconds or timing out).

For SymPy 15976, 16792 and 17655, pytest 4.6.11 with py 1.11.0 restores the old
`py.test` API. These pins passed collection of `test_comb_numbers.py` and a
`test_fibonacci` smoke test in each instance's derived official container;
this is not a full-suite compatibility claim. Instrumentation collection/runner
errors (exit 2, 3, 4 or 127) now fail preparation rather than publish a complete
artifact. They are not automatically added to the slow-file exclusion list.

```bash
python -m swebench.harness.namerts.prepare \
  --dataset_name SWE-bench/SWE-bench_Verified \
  --split test \
  --instance_ids astropy__astropy-12907 \
  --artifact_root namerts_artifacts \
  --total_timeout 3600
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
SymPy RTS golden and selected runners enable calculation caching explicitly
(`SYMPY_USE_CACHE=yes`) and omit `bin/test -C` / `--no-cache`. This exception
applies only to the derived RTS runner: the original upstream F2P/P2P script is
unchanged. Both sides must use the same cache mode; old uncached baselines cannot
be reused. The baseline records `calculation_cache=yes`.

Artifacts now use schema version 7 / runtime recipe 16. Older artifacts must
be prepared again; they lack complete instrumentation timings and the new
execution policy. The baseline stores `instrumentation_results`,
`slow_test_files`, the threshold, grouped output and stage timings. Parsed
failure identities normalize ephemeral `/tmp` paths and memory addresses before
golden/candidate comparison.

SymPy-derived runtimes install pytest in `testbed` for instrumentation
(7.4.4, or 6.2.5 for Python 3.6). This does not modify the official image:
golden and selected regression tests still use the official `bin/test` command.

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
  --timeout 3600
```

RTS commands inherit `--timeout` unless `--namerts-timeout` is explicitly set.
The example uses one hour. The upstream eval script and RTS commands each
receive their own timeout window; this is not a shared end-to-end deadline.

The harness first runs and grades the normal SWE-bench tests in the official
image, retaining its setup commit and configuration exactly as ordinary
evaluation does. It does not reset that checkout to the dataset base commit.
An upstream-unresolved patch does not load an artifact or start an RTS
container. For an upstream-resolved patch, the harness starts a derived runtime
container and selects affected tests from the prepared parent cache, using
the dataset-base candidate tree for selection only. Before selected tests, it
reapplies the official setup delta used by the golden baseline, preserving
configuration such as Sphinx's pytest `-rA`. A conflict with candidate edits is
reported as `candidate_setup` failure rather than overwriting the candidate.
It reuses the official image's `/testbed` editable installation, or reruns the official
install command for a non-editable project, then restores the original PR's test
files to the dataset base, applies the dataset test patch, and runs the selected
tests together in one official runner invocation. Cross-instance parallelism
is controlled by the ordinary `--workers` option (eight in this example).
The old `--namerts-workers` and `--namerts-per-file-timeout` options remain
accepted for compatibility but do not add test concurrency or per-file deadlines.

**Slow-file filtering applies only to the additional RTS regression scope.**
It never changes the dataset's original FAIL_TO_PASS / PASS_TO_PASS tests,
the official evaluation script, or upstream grading. Even if an original
dataset test is marked slow by instrumentation, ordinary harness evaluation
still executes it with its normal whole-script timeout.

The golden regression suite and selected candidate tests exclude the same
recorded slow files. Selection logs retain the unfiltered list and
`slow_excluded_tests`. GIS, fixture and example directories excluded by
NameRTS discovery are likewise unrelated to upstream test execution.
SymPy's `sympy/integrals/rubi/` is also excluded during discovery, matching
the project's default collection exclusion for this optional integration suite.
This project-specific rule lives beside the GIS/example/fixture filters in
`NameRTS/src/utils.py`; it is not a measured slow-test entry. Changing these
rules changes the NameRTS source fingerprint and requires fresh artifacts.
The same SymPy-specific skip list excludes `sympy/utilities/tests/test_pytest.py`:
historical versions import `Failed`, which is undefined in pytest mode. This
explicit exclusion applies only to RTS discovery, not official F2P/P2P tests.

Additional compatibility policy is static and version controlled in
`swebench/harness/namerts/instance_policy.json`. Project entries provide pinned
test dependencies (GitPython for Pylint, xmlschema for pytest); instance entries
provide `pytest_version`/`py_version` overrides and `skip_test_files`, a mapping
from repository-relative Python files to reasons. Compatibility skips are kept
separate from measured `slow_test_files`. They apply to instrumentation, the
golden RTS baseline and selected RTS tests, never the original required tests.
Changing compatibility policy requires fresh artifacts. Source inspection used
to identify affected instances is an offline maintenance activity, not runtime
heuristics or automatic suppression of new candidate errors.

RTS instrumentation, golden baselines and selected tests use warning-ignore
configuration: `PYTHONWARNINGS=ignore` and `-W ignore` for pytest (including
pytest arguments forwarded by tox). SymPy's `bin/test` explicitly enables
warning errors internally, so an RTS-only wrapper temporarily changes those
filter actions in its runner module to `ignore` and restores the original bytes
in `finally`. This edit happens only during test execution, never selection or
official F2P/P2P. No startup hooks, pytest plugins or warning-API monkeypatches
are added; NameRTS's existing capture bootstrap remains unchanged. Explicit
warning assertions/filters in individual tests retain their normal semantics.
Recipe 19 requires fresh runtime images and artifacts; recipe 18's startup-hook
collision made its dynamic caches incomplete, so they must not be reused.
Adding pinned dependencies changes only the
derived RTS image, not the official image, but can enable optional tests in that
derived environment. Review pins against the project's Python version.
Golden and candidate failures are compared using the official project parser's
test-case IDs, not a scope-dependent group exit identity. Collection errors,
unparseable nonzero exits, and empty parser results for a nonempty scope are
conservative errors, not matching baseline failures. A golden group that aborts
collection or times out cannot produce a valid artifact; a candidate group
that does so is unresolved. Neither is automatically retried. This deliberately
does not treat a partially executed full suite as a complete baseline.
Empty RTS selections run no project tests.

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
