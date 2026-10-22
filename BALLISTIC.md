# BallisticLA fork of LAPACK++

This fork carries LAPACK++ fixes that RandBLAS and RandLAPACK need before
upstream ([icl-utk-edu/lapackpp](https://github.com/icl-utk-edu/lapackpp))
merges and releases them. It is not a separate line of development. Every
carried patch is an upstream pull request. The fork is retired once all of
them are in an upstream release (see [Exit criteria](#exit-criteria)).

BLAS++ is carried the same way in
[BallisticLA/blaspp](https://github.com/BallisticLA/blaspp), whose
`BALLISTIC.md` lists the BLAS++ patches.

## Branches and tags

- `main` (the default branch) is upstream `master` plus one merged fork pull
  request per carried patch, plus the fork CI and this file.
- `master` is an exact mirror of upstream `master`. Nothing is committed to it.
- `ballistic-YYYY.MM.DD` tags (`.N` for a same-day re-tag) are annotated tags
  on `main` that downstream builds pin. The tag message lists the patch heads,
  their tree hashes and the CI run.
- `archive/fork-master-2026-08-07` is the fork's `master` before the reset.
- Tags matching `ballistic-*` and `archive/*` cannot be moved or deleted.

## Patch registry

The tree hash identifies a patch's content; a reworded commit keeps it.

| ID | Upstream | Head | Tree | Fork PR | State |
|---|---|---|---|---|---|
| L1 | [#92](https://github.com/icl-utk-edu/lapackpp/pull/92): `LAPACK_LIBRARIES` paths with spaces (the LAPACK++ half of blaspp#137) | `720217022faf38945ab76eb43beb0f5b7e660da2` | `90e189db53eb3239f76f4c49ddf855b21df2c664` | - | pending |
| L2 | [#91](https://github.com/icl-utk-edu/lapackpp/pull/91) (Riley Murray): `lapack_symbol_suffix` for symbol-suffixed ILP64 OpenBLAS | `1f85c68ec3bdfc7be4d6f72daa4443c764f1da5a` | `239f3e7189e366fbc1cc65416fb30eb74478bacf` | - | pending |
| L3 | PR to be opened: `gemqrt` with m = 0 or n = 0 takes `&work[0]` of an empty `std::vector` | - | - | - | pending |
| L4 | PR to be opened: a re-configure drops `LAPACK_VERSION`, `LAPACK_HAVE_LAPACKE` and other defines | - | - | - | pending |

States: `pending` (not in `main`), `carried` (merged into `main` through its
fork PR), `upstream` (merged upstream; still carried until a release has it),
`dropped`. L2 keeps its author's commits and authorship.

## Fork-only commits

`integration(<ids>)` commits resolve conflicts between carried patches.
`fixup(<id>)` commits fix a defect found in a carried patch. Each enters `main`
through its own fork PR and is never sent upstream as such.

None so far. Expected: `integration(L4, L2)`, because L4's re-configure cache
key borders L2's lines and must include L2's `lapack_symbol_suffix`.

## Known issues in carried patches

None recorded. Defects found in L2 are recorded here and in its fork PR, not
upstream.

## Exit criteria

The fork is retired when every carried patch is merged upstream and contained
in a tagged upstream release. RandBLAS and RandLAPACK then pin that upstream
tag, `main` is frozen, and the tags stay.

## Rebuilding main

Upstream squash-merges pull requests, so a carried commit never becomes an
upstream ancestor and `main` cannot be merged forward. When upstream moves:

1. Create a branch from the new upstream `master`.
2. Merge the head of every patch that is still unmerged upstream, in registry
   order, and re-apply the fork-only commits.
3. If upstream merged a modified version of one of our patches, compare its
   files on upstream `master` with our head before dropping ours. Anything
   upstream left out becomes a `fixup(<id>)`.
4. Run the fork CI on the branch (`workflow_dispatch` with its SHA) and wait
   for it to finish with every sub-check as expected.
5. Only then replace `main` with the branch and tag the result.

## The default branch: side effects

- Never use "Sync fork" or `gh repo sync --force` on `main`; either would drop
  the carried patches. Only `master` mirrors upstream.
- GitHub's "Contribute" button, and `gh pr create` without `-R`, target
  icl-utk-edu/lapackpp and offer everything on `main`. Fork PRs always name
  `-R BallisticLA/lapackpp --base main --head <branch>`; upstream PRs come from
  a branch based on upstream `master`.
- Upstream's CT and Variants workflows are disabled here (they need ICL's
  self-hosted runners). Only `ballistic-ci` runs.
- Upstream's `.github/workflows/configure.sh` clones upstream BLAS++ `master`
  unpinned; the fork CI does not use it.

## Fork CI

`.github/workflows/ballistic-ci.yml` and `.github/ballistic/ci.py` are
identical in both forks. A change to either goes through a fork PR in each
fork with identical content. While such a change has landed in only one fork,
runs there report the other fork's mismatching expectation entries as notices
and use their upstream defaults, so the second PR can follow.

**Triggers.** A pull request into `main`, a push to `main`, and
`workflow_dispatch` with `blaspp_ref`, `lapackpp_ref` (full SHAs) and `expect`
(below). Each run builds exactly two commits, fetched by SHA and printed in
the run summary: the commit under test from this fork (for a dispatch with an
empty ref, the commit the run was started from) and the other fork's `main`
(or the given ref).

**Lanes.** Each lane is one job; each records one or more sub-checks. A build
sub-check configures, builds and installs one library; checks the installed
`defines.h` and `<lib>Config.cmake`; builds and runs `examples/` against the
install; and runs the tester (`run_tests.py --quick --blas1 --blas2 --blas3`
for BLAS++, `--quick --host` for LAPACK++).

| Lane | Runner | Configuration | Sub-checks (owner) |
|---|---|---|---|
| 1-cmake, 1-make | ubuntu-24.04 | apt OpenBLAS LP64; CMake with `blas=auto`, and make | `.blaspp`, `.lapackpp` |
| 2 | ubuntu-24.04 | apt `libopenblas64` (ILP64, unsuffixed) | `.blaspp`; `.lapackpp` (L2: `-llapacke64`) |
| 3-cmake, 3-make | ubuntu-24.04 | conda-forge `libopenblas64_`, `blas_symbol_suffix=64_` | `.blaspp`; `.lapackpp` (L2) |
| 4a, 4b | ubuntu-24.04 | oneMKL ILP64 (Ubuntu `libmkl-dev`) as `BLAS_LIBRARIES` and `LAPACK_LIBRARIES` of both libraries, (a) as a semicolon list and (b) as one space-separated string of `-l` flags | `.blaspp`, `.lapackpp` |
| 5a | ubuntu-24.04 | OpenBLAS copied into a directory `blas dir`, given to BLAS++ as `BLAS_LIBRARIES` and `LAPACK_LIBRARIES` | `.blaspp`, `.lapackpp` (B1) |
| 5b | ubuntu-24.04 | the same copy, given to LAPACK++ as `LAPACK_LIBRARIES` | `.lapackpp` (L1) |
| 6 | ubuntu-24.04 | re-configure an existing build tree, then byte-compare `defines.h` and `<lib>Config.cmake` with a fresh configure | `.blaspp.identical` (B2a); `.blaspp.fortran`, `.blaspp.suffix`, `.blaspp.failed-then-fixed` (B2b); `.lapackpp.identical`; `.lapackpp.option`, `.lapackpp.failed-then-fixed` (L4) |
| 6b | ubuntu-24.04 | LAPACK++ built with `-D_GLIBCXX_ASSERTIONS` | `.lapackpp.sanity`; `.lapackpp.zero-dims` (L3) |
| 7a, 7b | macos-15 | new Accelerate, LP64 and ILP64. No LAPACKE, so build and consumer only | `.blaspp`, `.lapackpp` |
| 7c | macos-15 | LAPACK++ with `LAPACK_LIBRARIES="-framework Accelerate"`; build and consumer only | `.lapackpp` |
| 8a | windows-2022 | MSVC; oneMKL ILP64 from Intel's NuGet packages (pinned; SHA256-checked when downloaded, then cached), import libraries copied into a directory `intel mkl`, given to BLAS++ as `BLAS_LIBRARIES` | `.blaspp`, `.lapackpp` (B1) |
| 8b | windows-2022 | the same libraries at a path without spaces; LAPACK++ found in them, and LAPACK++ given the spaced copies as `LAPACK_LIBRARIES` | `.blaspp`, `.lapackpp`; `.lapackpp.spaced` (L1) |

Lane 6 scenarios: `identical` re-runs the same configure (BLAS++ also builds
LAPACK++ against the re-configured install); `fortran` changes `blas_fortran`;
`suffix` adds `blas_symbol_suffix=64_` after a configure without it failed;
`failed-then-fixed` names a library that appears only after the first
configure failed; `option` changes LAPACK++'s `lapack` option. Lane 6b runs
`gemqrt` with m = 0 (side R) and n = 0 (side L), where the workspace is empty,
and requires printed passing results. Upstream's tester also takes `&C[0]` of
its empty C when n = 0, so the side-L runs pass only with L3's tester change
as well as its library change.

**Expectations.** `.github/ballistic-expectations.yml` gives each sub-check an
expected outcome: `pass`, or `xfail:<id>` while patch `<id>` is not in `main`.
An entry lives in the fork of its owning patch; a sub-check without an owner
lives in the fork of the library it builds. The check reads both files at the
commits under test; a commit without the file (either fork) is taken as
unmodified upstream. The `check expectations` job fails on any mismatch, in
either direction: an expected pass that fails, or an `xfail` that passes.
`workflow_dispatch` can override the files with `expect=candidate` (every
sub-check passes) or `expect=baseline` (every patch-owned sub-check fails, the
rest pass).

**Testers.** Each sub-check's tester setting is the third field of its
`LANES` entry in `ci.py`: `GATE` (expect_tester true: the tester must build,
and its results are compared per routine), `RECORD` (Windows: run and
recorded, never gating, until a recorded run sets the expectation) or none
(macOS, where Accelerate has no LAPACKE). On Windows the testers do not
compile with MSVC upstream (testsweeper v2025.05.28 includes `<sys/time.h>`,
BLAS++'s `test/test.cc` includes `<unistd.h>`), so lanes 8a and 8b are build
and consumer checks for now.

Per-routine results are compared with `.github/ballistic-baseline/<sub-check>.json`
in the fork of the tested library. A routine that passed there must still run
and pass, and a new routine must pass. A routine recorded as failing may keep
failing only in the result rows that failed there, with an exit status equal
to its number of failed rows (so a crash is not a known failure). Lane 5's
sub-checks, which copy lane 1's OpenBLAS, are held to the `1-cmake.<lib>`
baseline until they have their own. Any other sub-check without a stored
baseline needs every routine to pass and, when it runs the default tester
arguments, every routine of `1-cmake.<lib>` to have run.
Baselines are written from a run's downloaded `results-*` artifacts with
`ci.py baseline --results <dir> --lib <lib> --dest .github/ballistic-baseline --run-url <url>`,
which re-parses the raw tester output, refuses sub-checks whose build, checks
or consumer failed, and records failing routines only with
`--accept-failures`.

**Relaxing the gate.** In a pull request, the check compares this fork's
expectations and baselines at the PR's base with its head. Flipping an
`xfail` to `pass` or adding a baseline is fine. Anything that relaxes the
gate (an entry leaving `pass` or disappearing; a baseline file or routine
disappearing; a baseline routine that no longer passes or fails in more rows)
fails the check unless the PR carries the label `ci-rules-change`; the summary
lists the changes either way. A changed `ci.py` or workflow is flagged as a
notice.

**Baseline.** The F0 pull request's first runs on unmodified upstream master
([BLAS++](https://github.com/BallisticLA/blaspp/actions/runs/39682095446),
[LAPACK++](https://github.com/BallisticLA/lapackpp/actions/runs/39682102757))
produced the stored per-routine baselines. Two findings there: `rotg` fails
with Ubuntu 24.04's OpenBLAS 0.3.26 in three complex rows (c n=500, z n=200,
z n=300; lanes 1-cmake, 1-make and 2; recorded as a known failure), and
upstream LAPACK++ does not find LAPACKE for unsuffixed ILP64 OpenBLAS, so lane
2's LAPACK++ tester is not built until L2 adds `-llapacke64`.

**Merge rule.** A fork PR is merged only after its `ballistic-ci` run has
finished and `check expectations` has passed. A patch PR changes its own
entries from `xfail:<id>` to `pass`; that proves the fix while every other
sub-check is held to the baseline. After a merge into one fork's `main`, a
`workflow_dispatch` run on the other fork's `main` confirms the pair. A run
re-used with "Re-run failed jobs" keeps its resolved commits; use "Re-run all
jobs" to pick up the other fork's new `main`.

**First merge (F0).** While a fork's `main` has no F0 yet, runs in the other
fork find no expectations or baselines for it. They treat that commit as
unmodified upstream: its owned sub-checks must fail and the rest pass, and its
tester routine failures are recorded as notes, since there is no baseline to
hold them to (BLAS++'s known `rotg` failure shows up this way in LAPACK++'s
F0 run). Either F0 can be merged first.

**Not covered yet.** The CUDA re-configure case (no GPU runners). A
`lapack_symbol_suffix` re-configure scenario arrives with the L4/L2
integration PR.
