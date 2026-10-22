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
| L1 | [#92](https://github.com/icl-utk-edu/lapackpp/pull/92): `LAPACK_LIBRARIES` paths with spaces, with the same normalization as B1 | `2e8bef5c6ac1b152f3fa4682b21c36ef4cf912ca` | `96acce118c01b6c270eb6acd36cb8f60e35ba57d` | carry/l1 | carried |
| L2 | [#91](https://github.com/icl-utk-edu/lapackpp/pull/91) (Riley Murray): `lapack_symbol_suffix` for symbol-suffixed ILP64 OpenBLAS | `1f85c68ec3bdfc7be4d6f72daa4443c764f1da5a` | `239f3e7189e366fbc1cc65416fb30eb74478bacf` | carry/l2 | carried |
| L3 | PR to be opened (fork branch `fix-gemqrt-empty-workspace`): `gemqrt` allocates at least one workspace entry (empty-vector UB at n = 0 / m = 0; root cause of BallisticLA/RandLAPACK#91, see BallisticLA/RandLAPACK#202); tester sizes V by side and allows zero dims | `9fd0e5e0cd72644c2ee04ba2d8acf7de0d8a29f2` | `a47ade3fa1af372f9d7d763822144e8b45e96d00` | carry/l3 | carried |
| L4 | PR to be opened (fork branch `fix-reconfigure-module-caches`): re-configure keeps `LAPACK_VERSION`, `LAPACK_HAVE_XBLAS`, `LAPACK_HAVE_LAPACKE`; module caches keyed on `blaspp_libraries`/`blaspp_defines`; no cached failures | `e6fb7f2f1379a9252aa622f478ce64c35fdca87f` | `0c70008f9f8a7db121497cee520caacb75763865` | carry/l4 | carried |
| L2F1 | none (fork-only `fixup(lapackpp#91)`): with a LAPACK suffix different from BLAS++'s, LAPACK++'s version and other probes must use the LAPACK suffix | - | - | - | pending |

States: `pending` (not in `main`), `carried` (merged into `main` through its
fork PR), `upstream` (merged upstream; still carried until a release has it),
`dropped`. L2 keeps its author's commits and authorship.

## Fork-only commits

`integration(<ids>)` commits resolve conflicts between carried patches.
`fixup(<id>)` commits fix a defect found in a carried patch. Each enters `main`
through its own fork PR and is never sent upstream as such.

- `integration(lapackpp#91)` `7755649`: adds `lapack_symbol_suffix` to the
  LAPACKFinder search-cache key that L4 introduces (L2-2). It sits on branch
  `integration-l4-pr91` (`b53405a`), which merges L4's branch twice
  (`2e9036e` at `5feda2a`, `b53405a` at `e6fb7f2`); `main` takes it with L2
  through `carry/l2`.

## Known issues in carried patches

Found while reviewing L2 (#91 at `1f85c68`) for this fork. They are recorded
here and in L2's fork PR, not upstream. Each was reproduced or read in the code
at that head. The registry and the fork-only commits above show whether a fix is in `main`.

| # | Issue | Fix |
|---|---|---|
| L2-1 | With `lapack_symbol_suffix` set and different from BLAS++'s suffix, LAPACKConfig's version, XBLAS and LAPACKE probes get only `blaspp_defines`, not the LAPACK suffix. Version detection fails, and `defines.h` lacks `LAPACK_VERSION`, `LAPACK_HAVE_XBLAS` and `LAPACK_HAVE_LAPACKE`; 81 of the 385 `.cc` files directly in `src/` are version-gated, `gemqrt` among them. Not affected: a suffix equal to BLAS++'s, or MKL, which exports both unsuffixed and `_64_` names (`dpstrf_` and `dpstrf_64_`). | L2F1; fork CI sub-check `10.lapackpp.defines` |
| L2-2 | `lapack_symbol_suffix` is not in LAPACKFinder's search cache key, so changing it on a re-configure reuses the previous search. | `integration(lapackpp#91)` |
| L2-3 | On the FindLAPACK path (`BLA_VENDOR` or `use_cmake_find_lapack`), `LAPACK_FORTRAN_SUFFIX` never reaches `lapackpp_defs_`, so the option has no effect there. | L2F1 |
| L2-4 | The Make build (`configure.py`) applies the suffix only in its OpenBLAS search and does not read BLAS++'s `blas_symbol_suffix`; the CHANGELOG's "honored automatically" holds for the CMake build only. | none; RandBLAS and RandLAPACK build with CMake |
| L2-5 | The tester's CBLAS names (`test/cblas_wrappers.hh`) take `LAPACK_FORTRAN_SUFFIX` before `BLAS_FORTRAN_SUFFIX`, although CBLAS comes from the BLAS library. | none; tester only, and no CI lane builds the tester with two different suffixes |
| L2-6 | In `include/lapack/mangling.h`, a suffix with no mangling define falls back to `ADD_`, while without a suffix the same case is an `#error`. | none; every configure sets a mangling define |
| L2-7 | `config/lapacke_potrf.cc` is edited, but no probe uses it (LAPACKConfig and `config/lapack.py` use `lapacke_pstrf.cc`). | none; harmless |
| L2-8 | No check that the generated tester headers (`test/lapack_suffix.hh`, `test/lapacke_suffix.hh`) match `tools/lapacke_suffix_gen.py`. They regenerate byte-identically at `1f85c68`. | none |
| L2-9 | Including `lapacke.h` before `lapack/fortran.h` (or `lapack/mangling.h`) defines an unsuffixed `LAPACK_GLOBAL` first, and `mangling.h` keeps it. | none; RandLAPACK does not include `lapacke.h` |
| L2-10 | With `lapack_symbol_suffix` empty, LAPACK++ inherits BLAS++'s `blas_symbol_suffix` (`config/config.h`, `include/lapack/mangling.h`), and no setting asks for unsuffixed LAPACK names. So a BLAS++ built on a suffixed library (e.g. `libopenblas64_` with `64_`), paired through `LAPACK_LIBRARIES` with a separate, unsuffixed ILP64 LAPACK, binds `dpstrf_64_` where master bound `dpstrf_`. The given LAPACK is then bypassed if BLAS++'s library carries LAPACK, and the search fails otherwise. This is the documented inheritance ("honored automatically") seen from the other side. | none; RandBLAS and RandLAPACK use one library for BLAS and LAPACK |

Limitations of the fork CI are listed under "Known limitations" in
[Fork CI](#fork-ci).

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
4. Run the fork CI on the branch (`workflow_dispatch` from the branch, refs
   empty or its SHA) and wait for `check expectations (workflow_dispatch)` to
   finish with every sub-check as expected.
5. Only then replace `main`. The ruleset `main-requires-ci` (pull request
   required, required status `check expectations`, no bypass) blocks a
   force-push, so: set the ruleset's enforcement to disabled
   (`gh api -X PUT repos/BallisticLA/lapackpp/rulesets/<id> -f enforcement=disabled`),
   `git push --force-with-lease=main:<old main> <remote> <branch SHA>:refs/heads/main`,
   and set it back to `active` at once. Tag the result, and record the rebuild
   (date, old and new `main`, CI run, who disabled the ruleset) in the tag
   message and, through a fork PR, in this file.

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

**The gate.** The ruleset `main-requires-ci` on `main` requires a pull
request and the status `check expectations`. Only a `pull_request` run
reports under that name. A push or dispatch run reports as
`check expectations (push)` or `check expectations (workflow_dispatch)`: it
has no base to compare against and may test other commits of the other fork,
so it must not satisfy the requirement.

**Lanes.** Each lane is one job; each records one or more sub-checks. A build
sub-check configures, builds and installs one library; checks the installed
`defines.h` and `<lib>Config.cmake`; builds and runs `examples/` against the
install; and runs the tester (`run_tests.py --quick --blas1 --blas2 --blas3`
for BLAS++, `--quick --host` for LAPACK++). CMake builds give the testers'
FetchContent the testsweeper commit fetched for the run (tag v2025.05.28, the
one both libraries name); make builds use the same checkout.

| Lane | Runner | Configuration | Sub-checks (owner) |
|---|---|---|---|
| 1-cmake, 1-make | ubuntu-24.04 | apt OpenBLAS LP64; CMake with `blas=auto`, and make | `.blaspp`, `.lapackpp` |
| 2 | ubuntu-24.04 | apt `libopenblas64` (ILP64, unsuffixed) | `.blaspp`; `.lapackpp` (L2: `-llapacke64`) |
| 3-cmake, 3-make | ubuntu-24.04 | conda-forge `libopenblas64_`, `blas_symbol_suffix=64_` | `.blaspp`; `.lapackpp` (L2) |
| 4a, 4b | ubuntu-24.04 | oneMKL ILP64 (Ubuntu `libmkl-dev`) as `BLAS_LIBRARIES` and `LAPACK_LIBRARIES` of both libraries, (a) as a semicolon list and (b) as one space-separated string of `-l` flags; the exported `<lib>_libraries` and `<lib>_defines` must keep their baseline form | `.blaspp`, `.lapackpp` |
| 5a | ubuntu-24.04 | OpenBLAS copied into a directory `blas dir`, given to BLAS++ as `BLAS_LIBRARIES` and `LAPACK_LIBRARIES` | `.blaspp`, `.lapackpp` (B1) |
| 5b | ubuntu-24.04 | the same copy, given to LAPACK++ as `LAPACK_LIBRARIES` | `.lapackpp` (L1) |
| 6 | ubuntu-24.04 | re-configure an existing build tree, then byte-compare `defines.h` and `<lib>Config.cmake` with a fresh configure, whose `defines.h` must hold the expected defines | `.blaspp.identical` (B2a); `.blaspp.fortran`, `.blaspp.suffix`, `.blaspp.failed-then-fixed` (B2b); `.blaspp.lapack-generic` (B1); `.lapackpp.identical`; `.lapackpp.option`, `.lapackpp.failed-then-fixed`, `.lapackpp.findlapack-blaspp-swap` (L4) |
| 6b | ubuntu-24.04 | LAPACK++ built with `-D_GLIBCXX_ASSERTIONS` | `.lapackpp.sanity`; `.lapackpp.zero-dims` (L3) |
| 7a, 7b | macos-15 | new Accelerate, LP64 and ILP64. No LAPACKE, so build and consumer only | `.blaspp`, `.lapackpp` |
| 7c | macos-15 | `LAPACK_LIBRARIES="-framework Accelerate"` for LAPACK++, and the CMake list `-framework;Accelerate` for both libraries; build and consumer only | `.lapackpp`, `.blaspp.list`, `.lapackpp.list` |
| 8a | windows-2022 | MSVC; oneMKL ILP64 from Intel's NuGet packages (pinned; SHA256-checked when downloaded, then cached), import libraries copied into a directory `intel mkl`, given to BLAS++ as `BLAS_LIBRARIES` | `.blaspp`, `.lapackpp` (B1) |
| 8b | windows-2022 | the same libraries at a path without spaces; LAPACK++ found in them, and LAPACK++ given the spaced copies as `LAPACK_LIBRARIES` | `.blaspp`, `.lapackpp`; `.lapackpp.spaced` (L1) |
| 9 | ubuntu-24.04 | apt OpenBLAS; `LAPACK_LIBRARIES` for both libraries as an absolute path plus a flag in one space-separated string, and as bare names (`openblas m`); a re-configure re-passing the same value must not search for LAPACK again | `.blaspp.space-path`, `.lapackpp.space-path`, `.blaspp.bare-names`, `.lapackpp.bare-names`, `.blaspp.rerun`, `.lapackpp.rerun`; `.blaspp.rerun-identical` (B2a) |
| 10 | ubuntu-24.04 | reference BLAS (unsuffixed) for BLAS++, with only the reference LAPACK routines it needs; LAPACK++ given the reference LAPACK with every symbol renamed `<name>x_` and `lapack_symbol_suffix=x_` (static builds) | `.lapackpp.configure` (L2: the suffixed library is found); `.lapackpp.defines` (L2F1: `LAPACK_VERSION`) |

Lane 6 scenarios: `identical` re-runs the same configure (BLAS++ also builds
LAPACK++ against the re-configured install); `fortran` changes `blas_fortran`;
`suffix` adds `blas_symbol_suffix=64_` after a configure without it failed;
`failed-then-fixed` names a library that appears only after the first
configure failed; `option` changes LAPACK++'s `lapack` option;
`lapack-generic` configures BLAS++ with `lapack=generic` (a separate
`-llapack`) and re-configures it three times, the exported `blaspp_libraries`
matching a fresh configure after every run; `findlapack-blaspp-swap`
configures LAPACK++ on CMake's FindLAPACK path (`BLA_VENDOR=OpenBLAS`)
against an ILP64 BLAS++ (`libopenblas64`), then re-installs BLAS++ into the
same prefix as LP64 and re-configures LAPACK++, which must match a fresh
configure (with `LAPACK_VERSION` and `LAPACK_HAVE_LAPACKE`). Lane 6b runs
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
and pass, and print at least as many result rows (a routine compiled out of
the tester, as when `LAPACK_VERSION` goes missing, prints none yet passes). A
new routine must pass. A routine recorded as failing may keep failing only in
the result rows that failed there, with an exit status equal to its number of
failed rows (so a crash is not a known failure). Lane 5's sub-checks, which
copy lane 1's OpenBLAS, are held to the `1-cmake.<lib>` baseline until they
have their own. Any other sub-check without a stored baseline needs every
routine to pass and, when it runs the default tester arguments, every routine
of `1-cmake.<lib>` to have run and printed result rows where 1-cmake did.
Baselines are written from a run's downloaded `results-*` artifacts with
`ci.py baseline --results <dir> --lib <lib> --dest .github/ballistic-baseline --run-url <url>`,
which re-parses the raw tester output, refuses sub-checks whose build, checks
or consumer failed, and records failing routines only with
`--accept-failures`.

**Relaxing the gate.** In a pull request, the check compares this fork's
expectations and baselines at the PR's base with its head. Flipping an
`xfail` to `pass`, and adding passing routines to a baseline, tighten the gate.
Each of these fails the check unless the PR carries the label
`ci-rules-change`, and the summary lists them either way:
- an entry leaving `pass`, or disappearing;
- a baseline file or routine disappearing; a baseline routine that no longer
  passes, fails in more rows, or prints fewer result rows; a routine added to
  a baseline as failing; changed exports;
- a new baseline file that records any failing routine, or that lacks a
  routine (or its result rows) of the baseline the gate used for it until
  then (its lane-5 reference, else `1-cmake.<lib>`);
- a baseline recorded with tester arguments other than the sub-check's
  (`TESTER_ARGS` in `ci.py`), or with a result-row count that is not a
  non-negative integer; the check also rejects a baseline whose arguments
  differ from the run's;
- any added, removed or changed file under `.github/ballistic/` or
  `.github/workflows/` (once `main` has F0). This one is an acknowledgement
  step, not a barrier: the pull request's own `ci.py` runs the check, so a
  reviewer must read that diff. The workflow runs `ci.py` with `python -I`,
  so a module placed next to it cannot shadow the standard library.

**Baseline.** The F0 pull request's first runs on unmodified upstream master
([BLAS++](https://github.com/BallisticLA/blaspp/actions/runs/39682095446),
[LAPACK++](https://github.com/BallisticLA/lapackpp/actions/runs/39682102757))
established the baselines; the stored files were last regenerated, with
result-row counts and lane 4's exports, from runs
[39728443275](https://github.com/BallisticLA/blaspp/actions/runs/39728443275)
and [39728446200](https://github.com/BallisticLA/lapackpp/actions/runs/39728446200)
on the same upstream library code. Two findings: `rotg` fails with Ubuntu
24.04's OpenBLAS 0.3.26 in three complex rows (c n=500, z n=200, z n=300;
lanes 1-cmake, 1-make and 2; recorded as a known failure), and upstream
LAPACK++ does not find LAPACKE for unsuffixed ILP64 OpenBLAS, so lane 2's
LAPACK++ tester is not built until L2 adds `-llapacke64`.

**Merge rule.** A fork PR is merged only after its `ballistic-ci` run has
finished and `check expectations` has passed. A patch PR changes its own
entries from `xfail:<id>` to `pass`; that proves the fix while every other
sub-check is held to the baseline. After a merge into one fork's `main`, a
`workflow_dispatch` run on the other fork's `main` confirms the pair. A run
re-used with "Re-run failed jobs" keeps its resolved commits; use "Re-run all
jobs" to pick up the other fork's new `main`. Re-run any open pull request
whose run started before a merge in the other fork, before merging it: its
result was computed against the other fork's earlier `main`. The Phase 5
candidate-pair dispatch with `expect=candidate` is the backstop for entries
left unflipped. A dispatch run (such as the rebuild check) has no pull
request base, so it does not run the relaxing check.

**First merge (F0).** While a fork's `main` has no F0 yet, runs in the other
fork find no expectations or baselines for it. They treat that commit as
unmodified upstream: its owned sub-checks must fail and the rest pass, and its
tester routine failures are recorded as notes, since there is no baseline to
hold them to (BLAS++'s known `rotg` failure shows up this way in LAPACK++'s
F0 run). Either F0 can be merged first; after it merges, "Re-run all jobs" on
the other F0 pull request before merging it, so it is checked against the
merged F0.

**Known limitations.**
- An `xfail` is met by any failure of its sub-check, including a blocked
  phase or a failed precondition (for example a missing conda environment in
  lane 6), not only by the bug its patch fixes. This is transient by
  construction: the owning patch's flip to `pass` must then pass for real.
- The `ci-rules-change` label is read from the event that started the run.
  Re-running an older run that saw the label, after the label was removed,
  still passes. A second pull request with the same head commit shares the
  first one's `check expectations` status.
- The required status is up to date only within one fork. A pull request's
  run tests the other fork's `main` as it was when the run started, so a
  later merge in the other fork is not re-tested by the ruleset. Dispatch a
  run on the other fork's `main` after each merge, and run the final
  candidate pair before tagging.
- `actions/checkout`, `actions/cache`, `actions/upload-artifact` and
  `actions/download-artifact` v4 target Node.js 20; GitHub runs them on
  Node.js 24 with a deprecation warning.
- Not covered: the CUDA re-configure case (no GPU runners), and a
  re-configure that changes `lapack_symbol_suffix` (L2-2's fix). That case
  was checked locally only, before L2 was carried: every re-configure step
  gave the same `defines.h` and `lapackppConfig.cmake` as a fresh configure,
  or failed the same way.
