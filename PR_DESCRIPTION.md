# Add optional NameRTS regression testing to the SWE-bench harness

## Reference Issues/PRs

See also [#280](https://github.com/SWE-bench/SWE-bench/issues/280): patches that pass the current evaluation can still fail developer-written tests that were not executed.

## What does this implement/fix? Explain your changes.

### 1. Overview

This PR adds an **opt-in regression-test selection option, disabled by default**, to check for regressions beyond the original evaluation's test scope.

**With RTS disabled, the original evaluation workflow, test scope, and grading logic remain unchanged.** With RTS enabled, the harness still runs the original evaluation first. Only patches originally graded as resolved proceed to NameRTS selection and additional regression testing, whose results determine whether they remain resolved. Patches already graded as unresolved retain that result without test selection or additional tests.

NameRTS is included as a submodule: [NameRTS-SWEB](https://github.com/ZJU-CTAG/NameRTS-SWEB). From the harness's perspective, it provides two capabilities: initializing a reusable test-dependency cache, and returning test files to run given a candidate patch and that cache. See [our paper](https://dl.acm.org/doi/10.1145/3832218) for the algorithm.

### 2. Integration

#### One-time initialization per instance

1. Build a separately named `sweb.namerts.*` image from the official instance Docker image, with a NameRTS environment matching the instance's Python major and minor version.
2. Run instrumented tests sequentially by file on the pre-patch code with the official environment setup, generating a reusable cache and recording each file's execution time. Files that reach the 180-second limit are terminated and recorded as slow tests in `instance_policy.json`. Recorded slow files are skipped in the golden baseline, subsequent candidate selected tests, and future initialization runs—even when rebuilding images and artifacts from scratch.
3. Apply the golden patch and the dataset's test patch. Exclude the slow files and other configured exclusions, then execute the remaining full regression scope without instrumentation in a single invocation of the official runner. Save the failures as the baseline for candidate evaluation.
4. Store the cache, golden baseline, and metadata as an artifact. Reuse it when evaluating different candidate patches for the same instance, without repeating instrumentation or the golden full-suite baseline run.

Initialization, the golden baseline, and selected tests share the same file-level and function-level filtering rules. **These rules apply only to the additional RTS regression tests; they do not change the dataset's original FAIL_TO_PASS / PASS_TO_PASS tests.**

`swebench/harness/namerts/instance_policy.json` also records test exclusions identified during our experiments, including flaky tests and tests that depend heavily on external network services. These exclusions keep such tests out of the additional RTS evaluation scope and are reused across runs.

#### Candidate patch evaluation

1. Complete the original SWE-bench evaluation in the official image. Stop here if RTS is disabled or the original result is unresolved.
2. For patches that pass the original evaluation with RTS enabled, load the cache in the RTS image and run NameRTS selection to obtain the relevant test files.
3. Prepare the candidate test environment and apply the dataset's test patch. Run the selected tests together in one invocation of the official runner. Tests execute serially within each instance; instances can still run concurrently using the harness's existing workers.
4. Extract failing tests using the harness's existing log parser and compare them with the golden baseline:

   ```text
   New failures = candidate selected-test failures − golden baseline failures
   Final resolved = originally resolved AND RTS completed AND no new failures
   ```

The additional results are written to `namerts_regression` in the existing `report.json`, including the selected-test count, execution times, and new failures. Test timeouts and execution errors are not treated as passes. The three incompatible Python 2.7/3.5 instances in Verified continue to use the original evaluation workflow.

### 3. File changes

| Directory / file | Change | Purpose |
|---|---|---|
| `NameRTS/`, `.gitmodules` | Added | Include NameRTS-SWEB as a submodule; algorithm source remains in its own repository |
| `swebench/harness/namerts/__init__.py` | Added | RTS integration package |
| `swebench/harness/namerts/prepare.py` | Added | Initialization command, derived image builds, instrumentation cache and golden baseline generation |
| `swebench/harness/namerts/container_bridge.py` | Added | Invoke NameRTS inside containers, handle patches, and generate regression runner scripts |
| `swebench/harness/namerts/runtime.py` | Added | Host–container interaction, reuse of official installation and test commands, selection and selected-test execution |
| `swebench/harness/namerts/artifacts.py` | Added | Load and validate artifacts, normalize test results, and compare golden/candidate failure sets |
| `swebench/harness/namerts/evaluation.py` | Added | Coordinate the RTS evaluation workflow and result handling |
| `swebench/harness/namerts/instance_policy.json` | Added | Project/instance exclusions, including flaky and heavily network-dependent tests identified in experiments, slow-test files, and test dependency versions |
| `swebench/harness/namerts/policy.py` | Added | Read policies and persist newly identified slow tests safely across concurrent workers |
| `swebench/harness/run_evaluation.py` | Modified | Add optional parameters, invoke RTS after the original evaluation passes, and combine verdicts |
| `swebench/cli/evaluate.py` | Modified | Add RTS enablement and timeout options to `swebench eval` |
| `tests/test_namerts.py` | Added | Tests for initialization, artifacts, runners, grading, and compatibility with the original workflow |
| `tests/test_namerts_policy.py` | Added | Tests for exclusions, dependency configuration, and consistent filtering across the three stages |
| `tests/test_namerts_selection_workers.py` | Added | Tests for selection concurrency configuration |
| `tests/test_namerts_prepare.py` | Added | Tests for full-dataset/selected-instance initialization, unsupported-instance skipping, and artifact reuse |
| `.github/workflows/pytest.yaml` | Modified | Exclude the new RTS-specific tests from default CI; keep running the existing tests without checking out the submodule |
| `pyproject.toml` | Modified | Include `instance_policy.json` in the installed package |
| `.gitignore` | Modified | Ignore local artifacts and the policy write-lock file |

### 4. Usage

#### Installation and initialization

With Docker available, run the following from a checkout of this PR:

```bash
git submodule update --init --recursive
pip install -e .

python -m swebench.harness.namerts.prepare \
  --dataset_name SWE-bench/SWE-bench_Verified \
  --split test \
  --instance_ids astropy__astropy-12907 \
  --artifact_root namerts_artifacts \
  --total_timeout 3600
```

`--instance_ids` accepts multiple IDs. **Omit it to initialize the entire dataset split**:

```bash
python -m swebench.harness.namerts.prepare \
  --dataset_name SWE-bench/SWE-bench_Verified \
  --split test \
  --artifact_root namerts_artifacts \
  --total_timeout 3600
```

Initialization processes instances sequentially and automatically skips the three known incompatible instances. Existing artifacts are reused if validation passes and their matching images remain available. Running the same command again continues initialization for instances that are not yet ready. To reinitialize an existing instance, specify its ID and add `--force_rebuild true`; use the same approach if artifact validation fails or an image is missing.

Each instance's outputs are stored in `namerts_artifacts/<instance-id>/`, primarily as `manifest.json`, `parent_cache.tar.gz`, and `golden_baseline.json`.

| Initialization option | Purpose / default |
|---|---|
| `--dataset_name` | Dataset name or local path; required |
| `--instance_ids` | Optional; initialize only these instances when specified, or the entire split when omitted |
| `--artifact_root` | Output directory; defaults to `namerts_artifacts` |
| `--namerts_source` | NameRTS source directory; defaults to the submodule |
| `--total_timeout` / `--timeout` | Golden regression runner timeout in seconds; defaults to 1800, with 3600 used in the examples |
| `--image_pull_timeout` | Official image pull timeout in seconds; defaults to 900 |
| `--slow_test_threshold` | Duration threshold for classifying completed instrumented test files as slow; defaults to 180 seconds. The instrumentation hard limit remains 180 seconds per file |
| `--force_rebuild` | Defaults to `false`, reusing valid artifacts and matching images; explicitly set to `true` to rebuild RTS images and artifacts |

#### Enabling or disabling RTS

**RTS disabled: use the original command without any additional options.**

```bash
swebench eval verified \
  --predictions preds.jsonl \
  --instance astropy__astropy-12907 \
  --run-id model-standard \
  --timeout 3600
```

**RTS enabled: add `--namerts-artifact-root`.**

```bash
swebench eval verified \
  --predictions preds.jsonl \
  --instance astropy__astropy-12907 \
  --run-id model-rts \
  --namerts-artifact-root namerts_artifacts \
  --timeout 3600
```

To evaluate golden patches, replace `--predictions preds.jsonl` with `--gold`. For multiple instances, initialize their artifacts first, then use the existing `--workers` option to configure instance-level concurrency.

The original Python module entry point also remains available and allows explicit configuration of selection concurrency:

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Verified \
  --predictions_path preds.jsonl \
  --instance_ids astropy__astropy-12907 \
  --run_id model-rts-selection4 \
  --namerts_artifact_root namerts_artifacts \
  --namerts_selection_workers 4 \
  --max_workers 10 \
  --timeout 3600
```

| Evaluation option (Python module entry point) | Purpose / default |
|---|---|
| `--namerts_artifact_root` | Enables RTS and specifies the artifact directory; omit it to retain the original workflow |
| `--namerts_selection_workers` | Selection processes per instance; defaults to 4, or set to 1 for serial selection |
| `--namerts_timeout` | RTS command timeout; inherits the original `--timeout` by default |
| `--max_workers` | Existing option controlling instance-level concurrency |

`swebench eval` uses the hyphenated options `--namerts-artifact-root` and `--namerts-timeout`, with four selection processes by default. Use the Python module entry point above to customize selection concurrency. The legacy RTS compatibility parameters `namerts_workers` and `namerts_per_file_timeout` do not increase test concurrency or impose per-file deadlines on selected tests.

#### Python API

- `prepare_instance(client, instance, artifact_root, namerts_source, *, total_timeout=1800, slow_test_threshold=180, force_rebuild=False, ...)`: initialize one instance or reuse its valid artifact, returning the artifact directory.
- `run_instance(test_spec, pred, client, run_id, *, instance=..., namerts_artifact_root=..., namerts_selection_workers=4, namerts_timeout=None, ...)`: enable RTS through the existing single-instance evaluation interface.
- `run_instances(...)` and `main(...)`: also accept `namerts_artifact_root`, `namerts_selection_workers`, and `namerts_timeout` for batch evaluation.

#### Running RTS-specific tests

Default CI runs the existing tests and skips `tests/test_namerts*.py`, without initializing the NameRTS submodule. To run the RTS-specific tests manually:

```bash
git submodule update --init --recursive
pytest tests/test_namerts*.py
```

This exclusion applies only to CI; local pytest discovery is unchanged.

### 5. Experimental results

We ran initialization across all 500 SWE-bench Verified instances and compared local RTS-off/on evaluations for both golden patches and generated patches from the specified Gemini setting. Initialization artifacts were reused across patches. Selection used four processes, tests ran serially within each instance, and runner timeouts were set to 3600 seconds.

**All cumulative times below use serial accounting across instances: individual instance times are summed, without applying any speedup from concurrent instances.** RTS evaluation times combine the latest measurements for each stage; selection within each instance still uses four processes.

#### Initialization (excluding image pulls)

Initialization succeeded for 497 instances; the three incompatible instances used only the original evaluation workflow. Average times per instance were **2.54 minutes** for image building, **20.13 minutes** for instrumentation/cache generation, and **7.93 minutes** for the golden full-suite baseline, totaling **31.32 minutes** including other overhead. Cumulative initialization time was **259.48h**, with artifacts reusable across patches.

#### Golden patches

All 500 instances are included in the timing statistics.

| Metric | RTS off | RTS on |
|---|---:|---:|
| Evaluation time, excluding initialization | 8.91h | 47.39h |
| Average evaluation time per instance | 64.16s | 341.19s |
| Total time, including one-time initialization of the full dataset | 8.91h | 306.87h |
| Resolved (all 500 instances) | 485/500 (97.0%) | 485/500 (97.0%) |

RTS-off/on verdicts agree for every instance. The remaining 15 instances also fail the original evaluation and are not failures introduced by RTS.

#### Gemini patches

Input: [Gemini setting](https://github.com/SWE-bench/experiments/tree/main/evaluation/verified/20260901_mini-v2.4.2_gemini-3-5-flash). There are 441 nonempty patches and 59 missing patches; all verdicts come from local evaluation. All **441** patches are included in the timing statistics.

| Metric | RTS off | RTS on |
|---|---:|---:|
| Evaluation time, excluding initialization | 6.67h | 36.99h |
| Average evaluation time per instance | 54.48s | 301.98s |
| Total time, including one-time initialization of the full dataset | 6.67h | 296.48h |
| Resolved (all 500 instances) | 347/500 (69.4%) | 333/500 (66.6%) |

Fourteen instances changed from resolved to unresolved: 13 had new failures beyond the golden baseline, and one, SymPy-16597, timed out during testing.

Complete per-instance data: [results.latest.json (Google Drive)](https://drive.google.com/file/d/18wlkGWWPwehOvddykmo2LBBknFuHueaX/view?usp=drive_link).

## Any other comments?
