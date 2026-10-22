#!/usr/bin/env python3
# BallisticLA fork CI: lane runner and expectation checker.
#
# Fork-only infrastructure, never sent upstream. This file is identical in
# BallisticLA/blaspp and BallisticLA/lapackpp (.github/ballistic/ci.py);
# BALLISTIC.md in either repository explains the scheme.
#
#   ci.py resolve        pick the BLAS++ and LAPACK++ commits under test
#   ci.py lane LANE      build and test one lane, record its results, judge them
#   ci.py check          judge the results of every lane (the merge gate)
#   ci.py baseline       write per-routine baseline files from a run's results
#   ci.py selftest       unit tests for the output parser and the checker
#
# Standard library only; runs on the Linux, macOS and Windows runners.

import argparse
import collections
import difflib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

FORK = {
    'blaspp': 'https://github.com/BallisticLA/blaspp.git',
    'lapackpp': 'https://github.com/BallisticLA/lapackpp.git',
}
# testsweeper v2025.05.28, the tag both libraries' CMake builds fetch. The make
# builds get the same commit instead of configure.py's unpinned clone.
TESTSWEEPER = ('https://github.com/icl-utk-edu/testsweeper.git',
               '15408fc37ff2996af71f553837ffbc13a676f733')

# Carried patches and the repository each belongs to; BALLISTIC.md has the registry.
PATCHES = {
    'B1': 'blaspp', 'B2a': 'blaspp', 'B2b': 'blaspp',
    'L1': 'lapackpp', 'L2': 'lapackpp', 'L3': 'lapackpp', 'L4': 'lapackpp',
    'L2F1': 'lapackpp',  # fork-only fixup of L2 (lapackpp#91)
}

EXPECTATIONS = '.github/ballistic-expectations.yml'
# A pull request that relaxes expectations or baselines needs this label.
RULES_LABEL = 'ci-rules-change'
BASELINE_DIR = '.github/ballistic-baseline'

GATE = 'gate'      # expect_tester true: the tester must build, and its per-routine
                   # results are compared with the stored baseline
RECORD = 'record'  # best effort: run the tester if it builds, record, never gate
NONE = None        # no tester for this sub-check

# Lane catalog: lane -> (runner OS, [(sub-check, owning patch or None, tester)]).
# A sub-check's library is the second dot-separated field of its name. A
# sub-check with an owner fails until that patch is in the owner's fork main;
# one without an owner must always pass.
LANES = collections.OrderedDict([
    # 1: apt OpenBLAS LP64, the regression reference, both build systems.
    ('1-cmake', ('linux', [('1-cmake.blaspp', None, GATE), ('1-cmake.lapackpp', None, GATE)])),
    ('1-make', ('linux', [('1-make.blaspp', None, GATE), ('1-make.lapackpp', None, GATE)])),
    # 2: apt libopenblas64 (ILP64, unsuffixed symbols).
    # LAPACK++ needs L2 here too: it adds -llapacke64 to the LAPACKE search.
    ('2', ('linux', [('2.blaspp', None, GATE), ('2.lapackpp', 'L2', GATE)])),
    # 3: conda-forge libopenblas64_ (ILP64, symbols suffixed 64_); LAPACK++ needs L2.
    ('3-cmake', ('linux', [('3-cmake.blaspp', None, GATE), ('3-cmake.lapackpp', 'L2', GATE)])),
    ('3-make', ('linux', [('3-make.blaspp', None, GATE), ('3-make.lapackpp', 'L2', GATE)])),
    # 4: oneMKL ILP64 named through BLAS_LIBRARIES and LAPACK_LIBRARIES, (a) as
    # a semicolon list and (b) as one space-separated string of -l flags. Both
    # work upstream; B1 and L1 change how they are split, so both must keep
    # passing.
    ('4a', ('linux', [('4a.blaspp', None, GATE), ('4a.lapackpp', None, GATE)])),
    ('4b', ('linux', [('4b.blaspp', None, GATE), ('4b.lapackpp', None, GATE)])),
    # 5: OpenBLAS at a path with a space. (a) BLAS_LIBRARIES and LAPACK_LIBRARIES
    # given to BLAS++ (B1); (b) LAPACK_LIBRARIES given to LAPACK++ (L1).
    ('5a', ('linux', [('5a.blaspp', 'B1', GATE), ('5a.lapackpp', 'B1', GATE)])),
    ('5b', ('linux', [('5b.lapackpp', 'L1', GATE)])),
    # 6: re-configure an existing build tree; the generated defines.h and
    # <lib>Config.cmake must be byte-identical to a fresh configure.
    ('6', ('linux', [
        ('6.blaspp.identical', 'B2a', NONE),
        ('6.blaspp.fortran', 'B2b', NONE),
        ('6.blaspp.suffix', 'B2b', NONE),
        ('6.blaspp.failed-then-fixed', 'B2b', NONE),
        ('6.lapackpp.identical', None, NONE),
        ('6.lapackpp.option', 'L4', NONE),
        ('6.lapackpp.failed-then-fixed', 'L4', NONE),
        ('6.blaspp.lapack-generic', 'B1', NONE),
        ('6.lapackpp.findlapack-blaspp-swap', 'L4', NONE),
    ])),
    # 6b: LAPACK++ built with -D_GLIBCXX_ASSERTIONS; gemqrt with m = 0 and n = 0.
    ('6b', ('linux', [('6b.lapackpp.sanity', None, GATE), ('6b.lapackpp.zero-dims', 'L3', NONE)])),
    # 7: macOS new Accelerate; no LAPACKE, so build and consumer only.
    ('7a', ('macos', [('7a.blaspp', None, NONE), ('7a.lapackpp', None, NONE)])),
    ('7b', ('macos', [('7b.blaspp', None, NONE), ('7b.lapackpp', None, NONE)])),
    # 7c also passes LAPACK_LIBRARIES as the CMake list "-framework;Accelerate"
    # to both libraries.
    ('7c', ('macos', [('7c.lapackpp', None, NONE), ('7c.blaspp.list', None, NONE),
                      ('7c.lapackpp.list', None, NONE)])),
    # 8: MSVC + oneMKL ILP64 (NuGet) at a path with spaces. (a) BLAS_LIBRARIES
    # given to BLAS++ (B1); (b) LAPACK++ against the same libraries without
    # spaces, then given them at the spaced path as LAPACK_LIBRARIES (L1).
    # Testers are best effort until a recorded run sets their expectation.
    ('8a', ('windows', [('8a.blaspp', 'B1', RECORD), ('8a.lapackpp', 'B1', RECORD)])),
    ('8b', ('windows', [('8b.blaspp', None, RECORD), ('8b.lapackpp', None, RECORD),
                        ('8b.lapackpp.spaced', 'L1', NONE)])),
    # 9: apt OpenBLAS; LAPACK_LIBRARIES forms that work upstream and must keep
    # working, for BLAS++'s and LAPACK++'s LAPACKFinder: an absolute path plus
    # a flag as one space-separated string; bare library names; and a
    # re-configure re-passing the same value, which must not search again
    # (BLAS++'s byte-for-byte comparison needs B2a, as in lane 6).
    ('9', ('linux', [
        ('9.blaspp.space-path', None, NONE), ('9.lapackpp.space-path', None, NONE),
        ('9.blaspp.bare-names', None, NONE), ('9.lapackpp.bare-names', None, NONE),
        ('9.blaspp.rerun', None, NONE), ('9.blaspp.rerun-identical', 'B2a', NONE),
        ('9.lapackpp.rerun', None, NONE),
    ])),
    # 10: a LAPACK whose symbols carry a suffix BLAS++'s BLAS does not:
    # reference BLAS unsuffixed, reference LAPACK with every symbol renamed
    # <name>x_, LAPACK++ given lapack_symbol_suffix=x_. L2 finds the suffixed
    # library (LAPACKFinder probes with the suffix); its fork fixup L2F1 makes
    # LAPACKConfig's probes, LAPACK_VERSION among them, use it too.
    ('10', ('linux', [('10.lapackpp.configure', 'L2', NONE), ('10.lapackpp.defines', 'L2F1', NONE)])),
])

SUB = collections.OrderedDict()
for _lane, (_os, _subs) in LANES.items():
    for _sc, _owner, _tester in _subs:
        SUB[_sc] = {'lane': _lane, 'os': _os, 'lib': _sc.split('.')[1],
                    'owner': _owner, 'tester': _tester}

# Sub-checks that test the same BLAS library as another sub-check, and
# without their own baseline are held to that one's (lane 5 copies the apt
# OpenBLAS of lane 1 to a path with a space).
REFERENCE = {
    '5a.blaspp': '1-cmake.blaspp',
    '5a.lapackpp': '1-cmake.lapackpp',
    '5b.lapackpp': '1-cmake.lapackpp',
}

# Sub-checks whose baselines also hold the exported <lib>_libraries and
# <lib>_defines: lane 4 passes BLAS_LIBRARIES and LAPACK_LIBRARIES in both
# forms, and the form consumers receive must not change.
EXPORTS_CHECKED = ('4a.blaspp', '4a.lapackpp', '4b.blaspp', '4b.lapackpp')

BLASPP_TESTS = ['--quick', '--blas1', '--blas2', '--blas3']
LAPACKPP_TESTS = ['--quick', '--host']
SHA_RE = re.compile(r'^[0-9a-f]{40}$')


def repo_of(sc):
    """Repository whose expectations file holds sub-check sc."""
    owner = SUB[sc]['owner']
    return PATCHES[owner] if owner else SUB[sc]['lib']


def other(lib):
    return 'lapackpp' if lib == 'blaspp' else 'blaspp'


def gh_out(name, value):
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f'{name}={value}\n')


def summary(text):
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(text + '\n')


def die(msg):
    print(f'::error::{msg}')
    sys.exit(2)


# ---------------------------------------------------------------- processes

def _kill_tree(p):
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        p.kill()


def run(cmd, log, cwd=None, env=None, timeout=3600, echo=False, stdout_path=None):
    """Run cmd; append its output to log (and to the console if echo).
    With stdout_path, stdout goes there and stderr to the log. Returns the
    exit status; 124 on timeout, 127 if the program cannot start."""
    cmd = [str(c) for c in cmd]
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, 'a', encoding='utf-8', errors='replace') as f:
        f.write('$ ' + ' '.join(cmd) + (f'   (in {cwd})' if cwd else '') + '\n')
        f.flush()
        if echo:
            print('$ ' + ' '.join(cmd), flush=True)
        kw = {}
        if os.name != 'nt':
            kw['start_new_session'] = True
        out = open(stdout_path, 'wb') if stdout_path else None
        try:
            p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=out if out else subprocess.PIPE,
                                 stderr=subprocess.PIPE if out else subprocess.STDOUT, **kw)
        except OSError as e:
            f.write(f'cannot run: {e}\n')
            if out:
                out.close()
            return 127
        stream = p.stderr if out else p.stdout

        def pump():
            # Never let a write error stop the reader: the child would then
            # block on a full pipe until the timeout.
            for raw in iter(stream.readline, b''):
                line = raw.decode('utf-8', 'replace')
                try:
                    f.write(line)
                    if echo:
                        sys.stdout.write(line)
                except Exception:
                    pass
            f.flush()
        t = threading.Thread(target=pump)
        t.start()
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(p)
            p.wait()
            rc = 124
            f.write(f'[timed out after {timeout} s]\n')
        t.join()
        if out:
            out.close()
        f.write(f'[exit {rc}]\n')
    if echo:
        sys.stdout.flush()
    return rc


def tail(path, n=60):
    try:
        lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return ''
    return '\n'.join(lines[-n:])


def fetch(url, sha, dest, log):
    """Fetch exactly commit sha from url into dest; verify HEAD."""
    if not SHA_RE.match(sha):
        raise RuntimeError(f'not a full commit SHA: {sha!r}')
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if run(['git', 'init', '-q', dest], log) != 0:
        raise RuntimeError('git init failed')
    for attempt in range(3):
        if run(['git', '-C', dest, 'fetch', '-q', '--depth', '1', url, sha], log, timeout=600) == 0:
            break
        time.sleep(10 * (attempt + 1))
    else:
        raise RuntimeError(f'cannot fetch {url} {sha}')
    if run(['git', '-C', dest, '-c', 'advice.detachedHead=false', 'checkout', '-q', 'FETCH_HEAD'], log) != 0:
        raise RuntimeError('git checkout failed')
    head = subprocess.run(['git', '-C', str(dest), 'rev-parse', 'HEAD'], capture_output=True,
                          text=True).stdout.strip()
    if head != sha:
        raise RuntimeError(f'{url}: HEAD is {head}, expected {sha}')
    print(f'fetched {url} @ {sha}')


def ls_remote_main(url):
    for attempt in range(4):
        r = subprocess.run(['git', 'ls-remote', url, 'refs/heads/main'], capture_output=True, text=True)
        sha = r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else ''
        if SHA_RE.match(sha):
            return sha
        time.sleep(10 * (attempt + 1))
    die(f'cannot resolve main of {url}: {r.stderr.strip()}')


# ------------------------------------------------------- tester output parser

def parse_run_tests(stdout_text, xml_text, exit_code, test_cmd):
    """Turn run_tests.py output into per-routine results.

    run_tests.py prints each tester command (starting with test_cmd), the
    tester's output, then a line 'pass' or 'FAILED: exit code N'. The key is
    the routine plus its --type value, with '#2', '#3' for repeats.
    `details[key]['n']` counts the result rows the tester printed (a routine
    compiled out of the tester prints none, yet passes). For a failed command,
    `details` also records the tester's exit status and its failed result rows
    ('#<row> <first two columns>', in the order printed), so a known failure
    can be held to the rows that failed before. The result is
    cross-checked against the --xml report and the exit status of
    run_tests.py (the number of failed commands).
    Returns (routines, details, error); error is None when all agree."""
    prefix = test_cmd + ' '
    entries, current, body = [], None, []
    for line in stdout_text.splitlines():
        if line.startswith(prefix):
            if current is not None:
                return {}, {}, f'no result line for: {current}'
            current, body = line, []
        elif line == 'pass' or line.startswith('FAILED: exit code'):
            if current is None:
                return {}, {}, f'result line without a command: {line}'
            entries.append((current, 'pass' if line == 'pass' else 'fail', line, body))
            current = None
        elif current is not None:
            body.append(line)
    if current is not None:
        return {}, {}, f'run_tests.py stopped during: {current}'
    if not entries:
        return {}, {}, 'run_tests.py ran no routines'
    routines, details, seen = collections.OrderedDict(), {}, collections.Counter()
    for cmd, status, result, lines in entries:
        toks = cmd.split()
        routine = toks[-1]
        types = toks[toks.index('--type') + 1] if '--type' in toks[:-1] else ''
        base = f'{routine} [{types}]' if types else routine
        seen[base] += 1
        key = base if seen[base] == 1 else f'{base} #{seen[base]}'
        routines[key] = status
        rows, n = [], 0
        for row in lines:
            cols = row.split()
            if len(cols) >= 3 and cols[-1] in ('pass', 'FAILED'):
                n += 1
                if cols[-1] == 'FAILED':
                    rows.append(f'#{n} {cols[0]} {cols[1]}')
        details[key] = {'n': n}
        if status == 'fail':
            m = re.match(r'FAILED: exit code (-?\d+)', result)
            details[key].update(exit=int(m.group(1)) if m else None, rows=rows)
    if xml_text is None:
        return routines, details, 'run_tests.py wrote no XML report'
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return routines, details, f'unreadable XML report: {e}'
    from_xml = collections.Counter(
        (tc.get('name'), 'fail' if tc.find('failure') is not None else 'pass')
        for tc in root.iter('testcase'))
    from_out = collections.Counter((e[0].split()[-1], e[1]) for e in entries)
    if from_xml != from_out:
        return routines, details, ('console output and XML report disagree: '
                                   f'only in output {dict(from_out - from_xml)}, '
                                   f'only in XML {dict(from_xml - from_out)}')
    nfail = sum(1 for e in entries if e[1] == 'fail')
    if exit_code != nfail % 256:
        return routines, details, f'exit status {exit_code}, but {nfail} failed commands were parsed'
    return routines, details, None


def failing_blocks(text, test_cmd, limit=60):
    """The console output of each failed tester command, for the log."""
    blocks, cur = [], []
    for line in text.splitlines():
        if line.startswith(test_cmd + ' '):
            cur = [line]
        elif line.startswith('FAILED: exit code'):
            body = cur[:limit] + (['[...]'] if len(cur) > limit else [])
            blocks.append('\n'.join(body + [line]))
            cur = []
        elif cur:
            cur.append(line)
    return '\n\n'.join(blocks)


# ------------------------------------------------------------------- lanes

FILES = {
    'blaspp': ['include/blas/defines.h', 'blasppConfig.cmake'],
    'lapackpp': ['include/lapack/defines.h', 'lapackppConfig.cmake'],
}


class Runner:
    def __init__(self, lane, a):
        self.lane = lane
        self.osname = LANES[lane][0]
        self.win = os.name == 'nt'
        self.work = Path(a.work).resolve()
        self.out = Path(a.out).resolve()
        self.logs = self.out / 'logs'
        self.jobs = str(os.cpu_count() or 2)
        self.src = {k: self.work / 'src' / k for k in ('blaspp', 'lapackpp', 'testsweeper')}
        self.env = dict(os.environ)
        self.env.setdefault('OMP_NUM_THREADS', '4')
        if self.win:
            # MSVC, not a MinGW or Strawberry Perl g++ that is also on PATH.
            self.env['CC'], self.env['CXX'] = 'cl', 'cl'
        self.gen = ['-G', 'Ninja'] if shutil.which('ninja') else []
        ldflags = os.environ.get('LDFLAGS', '').strip()
        self.linker = ([f'-DCMAKE_EXE_LINKER_FLAGS={ldflags}', f'-DCMAKE_SHARED_LINKER_FLAGS={ldflags}']
                       if ldflags else [])
        self.res = {
            'lane': lane, 'os': self.osname, 'infra': 'running',
            'blaspp': {'url': a.blaspp_url, 'sha': a.blaspp_sha},
            'lapackpp': {'url': a.lapackpp_url, 'sha': a.lapackpp_sha},
            'testsweeper': TESTSWEEPER[1], 'subchecks': collections.OrderedDict(),
            'prereqs': collections.OrderedDict(),
        }
        for sc, _, _ in LANES[lane][1]:
            self.res['subchecks'][sc] = {'phases': collections.OrderedDict(), 'tester': None}

    # -- bookkeeping
    def rec(self, key):
        if key in self.res['subchecks']:
            return self.res['subchecks'][key]
        return self.res['prereqs'].setdefault(key, {'phases': collections.OrderedDict(), 'tester': None})

    def phase(self, key, name, ok, note='', t0=None):
        status = ok if isinstance(ok, str) else ('pass' if ok else 'fail')
        entry = {'status': status, 'note': note}
        if t0 is not None:
            entry['seconds'] = round(time.time() - t0)
        self.rec(key)['phases'][name] = entry
        print(f'[{key}] {name}: {status}' + (f' ({note})' if note else ''), flush=True)

    def blocked(self, key, names, why):
        for n in names:
            self.phase(key, n, 'blocked', why)

    def sh(self, cmd, logname, **kw):
        kw.setdefault('env', self.env)
        return run(cmd, self.logs / f'{logname}.log', **kw)

    def group(self, title):
        print(f'::group::{title}', flush=True)

    def endgroup(self):
        print('::endgroup::', flush=True)

    def runtime_env(self, dirs):
        """Environment for running programs: on Windows, DLL directories on PATH."""
        env = dict(self.env)
        if self.win:
            env['PATH'] = os.pathsep.join([str(d) for d in dirs] + [env.get('PATH', '')])
        return env

    def build_env(self):
        env = dict(self.env)
        if self.win and not shutil.which('cp'):
            # The testers' POST_BUILD step copies run_tests.py with `cp`. Git's
            # coreutils go last on PATH so they cannot shadow MSVC's link.exe.
            git_usr = Path(r'C:\Program Files\Git\usr\bin')
            if git_usr.is_dir():
                env['PATH'] = env.get('PATH', '') + os.pathsep + str(git_usr)
        return env

    # -- sources
    def fetch_sources(self):
        self.group('fetch sources')
        fetch(self.res['blaspp']['url'], self.res['blaspp']['sha'], self.src['blaspp'], self.logs / 'fetch.log')
        fetch(self.res['lapackpp']['url'], self.res['lapackpp']['sha'], self.src['lapackpp'], self.logs / 'fetch.log')
        fetch(TESTSWEEPER[0], TESTSWEEPER[1], self.src['testsweeper'], self.logs / 'fetch.log')
        self.endgroup()

    # -- CMake builds
    def cmake_lib(self, key, lib, name, args, deps=(), checks=(), consumer=True,
                  env=None, tester_args=None, tester=None):
        """Configure, build, install one library with CMake; then checks,
        consumer (examples/ against the install) and tester. Returns the
        install prefix, or None if the build failed."""
        if key in SUB:
            tester = SUB[key]['tester']
        key = key or f'prereq:{name}'
        bdir, prefix = self.work / 'build' / name, self.work / 'install' / name
        for d in (bdir, prefix):
            shutil.rmtree(d, ignore_errors=True)
        if any(d is None for d in deps):
            self.blocked(key, ['build'], 'a prerequisite build failed')
            return None
        env = env or self.build_env()
        self.group(f'{key}: build {lib}')
        t0 = time.time()
        cfg = ['cmake', '-S', self.src[lib], '-B', bdir, *self.gen, '-DCMAKE_BUILD_TYPE=Release',
               '-Dcolor=no', f'-DCMAKE_INSTALL_PREFIX={prefix}',
               f'-Dbuild_tests={"yes" if tester else "no"}', *self.linker, *args]
        if tester:
            # The testers' FetchContent uses the testsweeper commit fetched above
            # (the same tag), instead of cloning it again.
            cfg.append('-DFETCHCONTENT_SOURCE_DIR_TESTSWEEPER='
                       + str(self.src['testsweeper']).replace('\\', '/'))
        if deps:
            cfg.append('-DCMAKE_PREFIX_PATH=' + ';'.join(str(d) for d in deps))
        ok, note = self.sh(cfg, f'{name}.configure', env=env, echo=True) == 0, 'configure failed'
        if ok:
            ok, note = self.sh(['cmake', '--build', bdir, '--target', lib, '--parallel', self.jobs],
                               f'{name}.build', env=env) == 0, 'library build failed'
        tester_built = False
        if ok and tester:
            tester_built = self.sh(['cmake', '--build', bdir, '--target', 'tester', '--parallel', self.jobs],
                                   f'{name}.tester-build', env=env) == 0
        if ok:
            ok, note = self.sh(['cmake', '--install', bdir], f'{name}.install', env=env) == 0, 'install failed'
        if not ok:
            # The configure output is already on the console.
            for step in ('install', 'build'):
                log = self.logs / f'{name}.{step}.log'
                if log.exists():
                    print(tail(log, 80))
                    break
        self.endgroup()
        self.phase(key, 'build', ok, '' if ok else note, t0)
        dll_dirs = [prefix / 'bin'] + [Path(d) / 'bin' for d in deps if d]
        if not ok:
            if checks:
                self.blocked(key, ['checks'], 'build failed')
            if consumer:
                self.blocked(key, ['consumer'], 'build failed')
            return None
        self.rec(key)['exports'] = exports_of(lib, prefix, self.work)
        ctx = {'lib': lib, 'prefix': prefix, 'bdir': bdir, 'test_dir': bdir / 'test' if tester_built else None}
        self.run_checks(key, checks, ctx)
        if consumer:
            self.cmake_consumer(key, lib, name, [prefix] + list(deps), dll_dirs)
        if tester:
            self.run_tester(key, lib, bdir / 'test', tester_built,
                            tester_args or (tester_args_of(key) if key in SUB else default_tests(lib)),
                            dll_dirs + self.dll_dirs_under(bdir))
        return prefix

    def dll_dirs_under(self, root):
        if not self.win:
            return []
        return sorted({p.parent for p in Path(root).rglob('*.dll')})

    def cmake_consumer(self, key, lib, name, prefixes, dll_dirs):
        self.group(f'{key}: consumer (examples/)')
        t0 = time.time()
        edir = self.work / 'build' / f'{name}-examples'
        shutil.rmtree(edir, ignore_errors=True)
        env = self.runtime_env(dll_dirs)
        ok = self.sh(['cmake', '-S', self.src[lib] / 'examples', '-B', edir, *self.gen,
                      '-DCMAKE_BUILD_TYPE=Release', *self.linker,
                      '-DCMAKE_PREFIX_PATH=' + ';'.join(str(p) for p in prefixes)],
                     f'{name}.consumer', env=env, echo=True) == 0
        ok = ok and self.sh(['cmake', '--build', edir, '--parallel', self.jobs], f'{name}.consumer', env=env) == 0
        ok = ok and self.sh(['ctest', '--output-on-failure'], f'{name}.consumer', cwd=edir, env=env) == 0
        if not ok:
            print(tail(self.logs / f'{name}.consumer.log', 60))
        self.endgroup()
        self.phase(key, 'consumer', ok, '' if ok else 'examples did not configure, build or run', t0)

    # -- make builds (configure.py + GNUmakefile, in the source tree)
    def make_lib(self, key, lib, conf, deps_ok=True, checks=(), consumer=True):
        tester = SUB[key]['tester']
        src, prefix = self.src[lib], self.work / 'install' / 'make'
        if not deps_ok:
            self.blocked(key, ['build'], 'a prerequisite build failed')
            return False
        self.group(f'{key}: build {lib} (make)')
        t0 = time.time()
        name = f'{self.lane}-{lib}'
        ok = self.sh([sys.executable, 'configure.py', *conf, f'prefix={prefix}'], f'{name}.configure',
                     cwd=src, echo=True) == 0
        note = 'configure.py failed'
        if not ok and (src / 'config' / 'log.txt').exists():
            print(tail(src / 'config' / 'log.txt', 80))
        if ok:
            # Serial for BLAS++: its build also builds testsweeper, whose
            # nested configure races under make -j.
            jobs = '-j1' if lib == 'blaspp' else f'-j{self.jobs}'
            ok, note = self.sh(['make', jobs, 'lib'], f'{name}.build', cwd=src) == 0, 'make lib failed'
        tester_built = False
        if ok and tester:
            tester_built = self.sh(['make', f'-j{self.jobs}', 'tester'], f'{name}.tester-build', cwd=src) == 0
        if ok:
            ok, note = self.sh(['make', 'install'], f'{name}.install', cwd=src) == 0, 'make install failed'
        if not ok:
            print(tail(self.logs / f'{name}.build.log', 80))
        self.endgroup()
        self.phase(key, 'build', ok, '' if ok else note, t0)
        if not ok:
            if checks:
                self.blocked(key, ['checks'], 'build failed')
            if consumer:
                self.blocked(key, ['consumer'], 'build failed')
            return False
        ctx = {'lib': lib, 'prefix': prefix, 'bdir': src, 'test_dir': src / 'test' if tester_built else None}
        self.run_checks(key, checks, ctx)
        if consumer:
            self.group(f'{key}: consumer (examples/, pkg-config)')
            t0 = time.time()
            env = dict(self.env)
            env['PKG_CONFIG_PATH'] = os.pathsep.join(
                [str(prefix / 'lib' / 'pkgconfig')] + ([env['PKG_CONFIG_PATH']] if env.get('PKG_CONFIG_PATH') else []))
            ex = src / 'examples'
            ok = self.sh(['make', 'clean'], f'{name}.consumer', cwd=ex, env=env) == 0
            ok = ok and self.sh(['make'], f'{name}.consumer', cwd=ex, env=env) == 0
            ok = ok and self.sh(['make', 'test'], f'{name}.consumer', cwd=ex, env=env) == 0
            if not ok:
                print(tail(self.logs / f'{name}.consumer.log', 60))
            self.endgroup()
            self.phase(key, 'consumer', ok, '' if ok else 'examples did not build or run', t0)
        if tester:
            self.run_tester(key, lib, src / 'test', tester_built,
                            tester_args_of(key), [])
        return True

    # -- checks on the installed result
    def run_checks(self, key, checks, ctx):
        if not checks:
            return
        notes, ok = [], True
        for check in checks:
            good, note = check(self, ctx)
            ok = ok and good
            if not good:
                notes.append(note)
        self.phase(key, 'checks', ok, '; '.join(notes))

    # -- tester
    def run_tester(self, key, lib, test_dir, built, args, dll_dirs):
        rec = self.rec(key)
        exe = test_dir / ('tester.exe' if self.win else 'tester')
        if not built or not exe.exists():
            rec['tester'] = {'present': False, 'args': list(args)}
            print(f'[{key}] tester: not built', flush=True)
            return
        self.group(f'{key}: run_tests.py {" ".join(args)}')
        test_cmd = str(exe) if self.win else './tester'
        stem = self.out / 'tester' / key
        stem.parent.mkdir(parents=True, exist_ok=True)
        out_txt, xml = Path(f'{stem}.out.txt'), Path(f'{stem}.xml')
        timeout = 1800 if SUB.get(key, {}).get('tester') == RECORD else 2700
        rc = run([sys.executable, self.src[lib] / 'test' / 'run_tests.py', '--test', test_cmd,
                  '--xml', xml, *args], Path(f'{stem}.err.txt'), cwd=test_dir,
                 env=self.runtime_env(dll_dirs), timeout=timeout, stdout_path=out_txt)
        text = out_txt.read_text(encoding='utf-8', errors='replace') if out_txt.exists() else ''
        xml_text = xml.read_text(encoding='utf-8', errors='replace') if xml.exists() else None
        routines, details, err = parse_run_tests(text, xml_text, rc, test_cmd)
        if rc == 124:
            err = f'run_tests.py timed out after {timeout} s'
        failed = [k for k, v in routines.items() if v != 'pass']
        print(f'[{key}] tester: {len(routines)} routines, {len(failed)} failed'
              + (f': {", ".join(failed)}' if failed else '') + (f'; parse error: {err}' if err else ''))
        if failed or err:
            print(failing_blocks(text, test_cmd) if failed else tail(out_txt, 80))
        self.endgroup()
        rec['tester'] = {'present': True, 'args': list(args), 'exit': rc, 'routines': routines,
                         'details': details, 'parse_error': err}

    # -- re-configure scenarios
    def snapshot(self, lib, bdir, dest):
        dest.mkdir(parents=True, exist_ok=True)
        for rel in FILES[lib]:
            f = bdir / rel
            if f.exists():
                shutil.copy2(f, dest / Path(rel).name)

    def compare(self, lib, a, b):
        diffs = []
        for rel in FILES[lib]:
            fa, fb = a / rel, b / rel
            if not fa.exists() or not fb.exists():
                diffs.append(f'{rel}: missing in {"re-configured" if not fa.exists() else "fresh"} tree')
                continue
            ta = fa.read_text(encoding='utf-8', errors='replace').replace(str(a), '<BUILD>')
            tb = fb.read_text(encoding='utf-8', errors='replace').replace(str(b), '<BUILD>')
            if ta != tb:
                diffs.append(''.join(difflib.unified_diff(
                    tb.splitlines(True), ta.splitlines(True), f'fresh/{rel}', f're-configured/{rel}')))
        return diffs

    def reconfigure(self, key, lib, steps, final, deps=(), env=None, after=None, defines=(), compare=True,
                    only=None):
        """Run the configure steps in one build tree (each step: (args, 'ok' |
        'fail') or a callable), then compare defines.h and <lib>Config.cmake
        with a fresh configure using `final`. The fresh tree's defines.h must
        also define every name in `defines` ('NAME*' matches a prefix), so the
        comparison is against a known-good reference."""
        if any(d is None for d in deps):
            self.blocked(key, ['reconfigure', 'compare'], 'a prerequisite build failed')
            return
        env = env or self.env
        tree, fresh = self.work / 'build' / key, self.work / 'build' / f'{key}.fresh'
        prefix = self.work / 'install' / key
        snaps = self.out / 'reconfigure' / key
        for d in (tree, fresh, prefix, snaps):
            shutil.rmtree(d, ignore_errors=True)
        base = [*self.gen, '-DCMAKE_BUILD_TYPE=Release', '-Dcolor=no', '-Dbuild_tests=no',
                '-Dgpu_backend=none', f'-DCMAKE_INSTALL_PREFIX={prefix}']
        if deps:
            base.append('-DCMAKE_PREFIX_PATH=' + ';'.join(str(d) for d in deps))
        self.group(f'{key}: re-configure')
        t0, ok, note, i = time.time(), True, '', 0
        for step in steps:
            if callable(step):
                problem = step()
                if problem:
                    ok, note = False, problem
                    break
                continue
            args, want, check_output = (tuple(step) + (None,))[:3]
            log = self.logs / f'{key}.step{i}.log'
            rc = self.sh(['cmake', '-S', self.src[lib], '-B', tree, *base, *args], f'{key}.step{i}',
                         env=env, echo=True)
            self.snapshot(lib, tree, snaps / f'step{i}')
            if want == 'ok' and rc != 0:
                ok, note = False, f'configure step {i} failed'
            elif want == 'fail' and rc == 0:
                ok, note = False, f'precondition: configure step {i} was expected to fail but succeeded'
            elif check_output:
                problem = check_output(log.read_text(encoding='utf-8', errors='replace'))
                if problem:
                    ok, note = False, f'configure step {i}: {problem}'
            i += 1
            if not ok:
                break
        self.endgroup()
        self.phase(key, 'reconfigure', ok, note, t0)
        if not compare:
            return
        if not ok:
            self.blocked(key, ['compare'], 're-configure sequence did not complete')
            return
        self.group(f'{key}: fresh configure and compare')
        t0 = time.time()
        rc = self.sh(['cmake', '-S', self.src[lib], '-B', fresh, *base, *final], f'{key}.fresh',
                     env=env, echo=True)
        self.snapshot(lib, fresh, snaps / 'fresh')
        if rc != 0:
            self.endgroup()
            self.phase(key, 'compare', False, 'fresh configure failed', t0)
        else:
            f = fresh / FILES[lib][0]
            text = f.read_text(encoding='utf-8', errors='replace') if f.exists() else ''
            lacking = [d for d in defines if not re.search(
                r'#define\s+' + (re.escape(d[:-1]) if d.endswith('*') else re.escape(d) + r'\b'), text)]
            self.phase(key, 'fresh-defines', not lacking,
                       'fresh defines.h lacks ' + ', '.join(lacking) if lacking else '')
            if only:
                # Compare just these exported variables, after every step.
                cfg = Path(FILES[lib][1]).name
                want = config_vars(fresh / FILES[lib][1], only, self.work)
                diffs = []
                for d in sorted(p for p in snaps.glob('step*') if p.is_dir()):
                    got = config_vars(d / cfg, only, self.work)
                    if got != want:
                        diffs.append(f'{d.name}: {got} != fresh {want}')
                diffs = [f'{FILES[lib][1]}: ' + x for x in diffs]
            else:
                diffs = self.compare(lib, tree, fresh)
            if diffs:
                (snaps / 'diff.txt').write_text('\n'.join(diffs), encoding='utf-8')
                print('\n'.join(diffs))
            self.endgroup()
            self.phase(key, 'compare', not diffs,
                       '' if not diffs else 'differs from a fresh configure: '
                       + ', '.join(rel for rel in FILES[lib] if any(rel in d for d in diffs)), t0)
        if after:
            after(tree, prefix)

    # -- result file
    def write(self):
        self.out.mkdir(parents=True, exist_ok=True)
        with open(self.out / 'result.json', 'w', encoding='utf-8') as f:
            json.dump(self.res, f, indent=1)


# Checks: callables (runner, ctx) -> (ok, note).

def _defines(ctx):
    sub = 'blas' if ctx['lib'] == 'blaspp' else 'lapack'
    f = ctx['prefix'] / 'include' / sub / 'defines.h'
    return f.read_text(encoding='utf-8', errors='replace') if f.exists() else None


def defines_has(*names):
    def check(r, ctx):
        text = _defines(ctx)
        if text is None:
            return False, 'installed defines.h missing'
        missing = [n for n in names if not re.search(r'#define\s+' + re.escape(n) + r'\b', text)]
        return not missing, 'defines.h lacks ' + ', '.join(missing)
    return check


def defines_has_prefix(stem):
    def check(r, ctx):
        text = _defines(ctx)
        if text is None:
            return False, 'installed defines.h missing'
        return (re.search(r'#define\s+' + re.escape(stem), text) is not None,
                f'defines.h has no {stem}* define')
    return check


def defines_lacks(*names):
    def check(r, ctx):
        text = _defines(ctx)
        if text is None:
            return False, 'installed defines.h missing'
        present = [n for n in names if re.search(r'#define\s+' + re.escape(n) + r'\b', text)]
        return not present, 'defines.h defines ' + ', '.join(present)
    return check


def config_has(needle):
    def check(r, ctx):
        found = list(Path(ctx['prefix']).rglob(f'{ctx["lib"]}Config.cmake'))
        if not found:
            return False, f'installed {ctx["lib"]}Config.cmake missing'
        text = found[0].read_text(encoding='utf-8', errors='replace')
        return needle in text, f'{ctx["lib"]}Config.cmake does not mention {needle!r}'
    return check


def ldd_tester(has=(), lacks=()):
    def check(r, ctx):
        if ctx['test_dir'] is None:
            return False, 'tester not built (ldd check)'
        out = subprocess.run(['ldd', str(ctx['test_dir'] / 'tester')], capture_output=True, text=True).stdout
        bad = [p for p in has if not re.search(p, out)] + [f'not {p}' for p in lacks if re.search(p, out)]
        return not bad, 'tester linkage: ' + ', '.join(bad)
    return check


def lapacke_suffixed(sfx):
    def check(r, ctx):
        if ctx['test_dir'] is None:
            return False, 'tester not built (nm check)'
        out = subprocess.run(['nm', '-D', '--undefined-only', str(ctx['test_dir'] / 'tester')],
                             capture_output=True, text=True).stdout
        syms = re.findall(r'\b(LAPACKE_\w+)$', out, re.M)
        bad = [s for s in syms if not s.endswith(sfx)]
        if not syms:
            return False, 'tester imports no LAPACKE symbols'
        return not bad, f'unsuffixed LAPACKE symbols: {", ".join(bad[:5])}'
    return check


def exports_of(lib, prefix, work):
    """`<lib>_libraries` and `<lib>_defines` as the installed <lib>Config.cmake
    exports them to consumers, with the work directory written as <WORK>."""
    found = sorted(Path(prefix).rglob(f'{lib}Config.cmake'))
    if not found:
        return None
    text = found[0].read_text(encoding='utf-8', errors='replace')
    out = {}
    for var in (f'{lib}_libraries', f'{lib}_defines'):
        m = re.search(r'set\(\s*' + re.escape(var) + r'\s+"([^"]*)"', text)
        out[var] = m.group(1).replace(str(work).replace('\\', '/'), '<WORK>').replace(str(work), '<WORK>') if m else None
    return out


def config_vars(path, names, work):
    """{name: value} of `set( name "value" )` lines in a <lib>Config.cmake."""
    path = Path(path)
    text = path.read_text(encoding='utf-8', errors='replace') if path.exists() else ''
    out = {}
    for var in names:
        m = re.search(r'set\(\s*' + re.escape(var) + r'\s+"([^"]*)"', text)
        out[var] = m.group(1).replace(str(work), '<WORK>') if m else None
    return out


def lapack_not_redetected(text):
    """For a re-configure with unchanged LAPACK settings: the LAPACK search
    must not run again. Its probe lines (' yes', ' no (...)') appear between
    'Looking for LAPACK libraries and options' and the search's result line."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if 'Looking for LAPACK libraries and options' in line:
            for nxt in lines[i + 1:]:
                if 'Found LAPACK library' in nxt or 'LAPACK library not found' in nxt:
                    break
                if re.search(r'\s(yes|no \()', nxt):
                    return 'the LAPACK search ran again: ' + nxt.strip()
    return None


BLAS_MANGLING = defines_has_prefix('BLAS_FORTRAN_')
LAPACK_LINUX = defines_has('LAPACK_HAVE_LAPACK', 'LAPACK_VERSION', 'LAPACK_HAVE_LAPACKE')


def system_openblas():
    """Absolute path of the system libopenblas.so."""
    found = subprocess.run(['gcc', '-print-file-name=libopenblas.so'], capture_output=True, text=True).stdout.strip()
    if not os.path.isabs(found):
        raise RuntimeError('libopenblas.so not found')
    return found


def spaced_openblas(r):
    """Copy the system OpenBLAS into a directory whose name has a space."""
    found = subprocess.run(['gcc', '-print-file-name=libopenblas.so'], capture_output=True, text=True).stdout.strip()
    if not os.path.isabs(found):
        raise RuntimeError('libopenblas.so not found')
    d = r.work / 'blas dir'
    d.mkdir(parents=True, exist_ok=True)
    real = os.path.realpath(found)
    for name in ('libopenblas.so', 'libopenblas.so.0'):
        shutil.copy2(real, d / name)
    return d / 'libopenblas.so'


def recipe(r):
    lane = r.lane
    lp64 = ['-Dblas=openblas', '-Dblas_int=int32']

    if lane == '1-cmake':
        # blas=auto (the default): the whole candidate list is probed.
        bp = r.cmake_lib('1-cmake.blaspp', 'blaspp', '1-cmake-blaspp', [],
                         checks=[BLAS_MANGLING, defines_lacks('BLAS_ILP64')])
        r.cmake_lib('1-cmake.lapackpp', 'lapackpp', '1-cmake-lapackpp', [], deps=[bp], checks=[LAPACK_LINUX])

    elif lane == '1-make':
        conf = ['blas=openblas', 'blas_int=int32']
        ok = r.make_lib('1-make.blaspp', 'blaspp', conf, checks=[BLAS_MANGLING, defines_lacks('BLAS_ILP64')])
        r.make_lib('1-make.lapackpp', 'lapackpp', conf, deps_ok=ok, checks=[LAPACK_LINUX])

    elif lane == '2':
        bp = r.cmake_lib('2.blaspp', 'blaspp', '2-blaspp', ['-Dblas=openblas', '-Dblas_int=int64'],
                         checks=[BLAS_MANGLING, defines_has('BLAS_ILP64')])
        r.cmake_lib('2.lapackpp', 'lapackpp', '2-lapackpp', [], deps=[bp],
                    checks=[LAPACK_LINUX, ldd_tester(has=[r'liblapacke64\.so'], lacks=[r'liblapacke\.so'])])

    elif lane == '3-cmake':
        bp = r.cmake_lib('3-cmake.blaspp', 'blaspp', '3-cmake-blaspp',
                         ['-Dblas=openblas', '-Dblas_int=int64', '-Dblas_symbol_suffix=64_'],
                         checks=[BLAS_MANGLING, defines_has('BLAS_ILP64', 'BLAS_FORTRAN_SUFFIX')])
        # The suffix goes only to BLAS++; LAPACK++ inherits it.
        r.cmake_lib('3-cmake.lapackpp', 'lapackpp', '3-cmake-lapackpp', [], deps=[bp],
                    checks=[LAPACK_LINUX, lapacke_suffixed('64_')])

    elif lane == '3-make':
        conf = ['blas=openblas', 'blas_int=int64']
        if os.environ.get('LDFLAGS'):
            conf.append('LDFLAGS=' + os.environ['LDFLAGS'])
        ok = r.make_lib('3-make.blaspp', 'blaspp', conf + ['blas_symbol_suffix=64_'],
                        checks=[BLAS_MANGLING, defines_has('BLAS_ILP64', 'BLAS_FORTRAN_SUFFIX')])
        r.make_lib('3-make.lapackpp', 'lapackpp', conf + ['lapack_symbol_suffix=64_'], deps_ok=ok,
                   checks=[LAPACK_LINUX, lapacke_suffixed('64_')])

    elif lane in ('4a', '4b'):
        libs = ['-lmkl_gf_ilp64', '-lmkl_gnu_thread', '-lmkl_core', '-lgomp', '-lpthread', '-lm', '-ldl']
        value = ';'.join(libs) if lane == '4a' else ' '.join(libs)
        # The same value as LAPACK_LIBRARIES, for both libraries: B1 and L1
        # change how LAPACKFinder splits it.
        bp = r.cmake_lib(f'{lane}.blaspp', 'blaspp', f'{lane}-blaspp',
                         [f'-DBLAS_LIBRARIES={value}', f'-DLAPACK_LIBRARIES={value}', '-Dblas_int=int64'],
                         checks=[BLAS_MANGLING, defines_has('BLAS_ILP64')])
        r.cmake_lib(f'{lane}.lapackpp', 'lapackpp', f'{lane}-lapackpp', [f'-DLAPACK_LIBRARIES={value}'],
                    deps=[bp], checks=[LAPACK_LINUX, ldd_tester(has=[r'libmkl_gf_ilp64\.so'])])

    elif lane == '5a':
        lib = spaced_openblas(r)
        bp = r.cmake_lib('5a.blaspp', 'blaspp', '5a-blaspp',
                         [f'-DBLAS_LIBRARIES={lib}', f'-DLAPACK_LIBRARIES={lib}'],
                         checks=[BLAS_MANGLING, config_has('blas dir/libopenblas.so')])
        r.cmake_lib('5a.lapackpp', 'lapackpp', '5a-lapackpp', [], deps=[bp], checks=[LAPACK_LINUX])

    elif lane == '5b':
        lib = spaced_openblas(r)
        bp = r.cmake_lib(None, 'blaspp', '5b-blaspp', lp64, consumer=False)
        r.cmake_lib('5b.lapackpp', 'lapackpp', '5b-lapackpp', [f'-DLAPACK_LIBRARIES={lib}'], deps=[bp],
                    checks=[LAPACK_LINUX, config_has('blas dir/libopenblas.so')])

    elif lane == '6':
        lane6(r)

    elif lane == '6b':
        bp = r.cmake_lib(None, 'blaspp', '6b-blaspp', lp64, consumer=False)
        # LAPACK_LINUX: without LAPACK_VERSION the gemqrt tester is compiled
        # out and would "pass" without calling gemqrt.
        pre = r.cmake_lib('6b.lapackpp.sanity', 'lapackpp', '6b-lapackpp', ['-DCMAKE_CXX_FLAGS=-D_GLIBCXX_ASSERTIONS'],
                          deps=[bp], consumer=False, checks=[LAPACK_LINUX])
        zero_dims(r, '6b.lapackpp.zero-dims', pre, r.work / 'build' / '6b-lapackpp' / 'test')

    elif lane in ('7a', '7b', '7c'):
        ilp64 = lane == '7b'
        args = ['-Dblas=accelerate', '-Dblas_int=' + ('int64' if ilp64 else 'int32')]
        checks = [BLAS_MANGLING, defines_has('ACCELERATE_NEW_LAPACK'),
                  defines_has('BLAS_ILP64') if ilp64 else defines_lacks('BLAS_ILP64')]
        if lane == '7c':
            bp = r.cmake_lib(None, 'blaspp', '7c-blaspp', args, consumer=False)
            r.cmake_lib('7c.lapackpp', 'lapackpp', '7c-lapackpp', ['-DLAPACK_LIBRARIES=-framework Accelerate'],
                        deps=[bp], checks=[defines_has('LAPACK_HAVE_LAPACK'), config_has('-framework Accelerate')])
            listed = '-DLAPACK_LIBRARIES=-framework;Accelerate'
            r.cmake_lib('7c.blaspp.list', 'blaspp', '7c-blaspp-list', args + [listed], checks=checks)
            r.cmake_lib('7c.lapackpp.list', 'lapackpp', '7c-lapackpp-list', [listed], deps=[bp],
                        checks=[defines_has('LAPACK_HAVE_LAPACK')])
        else:
            bp = r.cmake_lib(f'{lane}.blaspp', 'blaspp', f'{lane}-blaspp', args, checks=checks)
            r.cmake_lib(f'{lane}.lapackpp', 'lapackpp', f'{lane}-lapackpp', [], deps=[bp],
                        checks=[defines_has('LAPACK_HAVE_LAPACK')])

    elif lane in ('8a', '8b'):
        def mkl(kind):
            root = os.environ.get(f'BCI_MKL_{kind}')
            iomp = os.environ.get(f'BCI_IOMP_{kind}')
            if not root or not iomp:
                raise RuntimeError(f'BCI_MKL_{kind} / BCI_IOMP_{kind} not set by the workflow')
            libs = [f'{root}/lib/mkl_intel_ilp64_dll.lib', f'{root}/lib/mkl_intel_thread_dll.lib',
                    f'{root}/lib/mkl_core_dll.lib', f'{iomp}/libiomp5md.lib']
            return [p.replace('\\', '/') for p in libs]
        common = ['-DBUILD_SHARED_LIBS=ON', '-Duse_cmake_find_blas=false', '-Dblas_int=int64',
                  '-Dblas_threaded=true', '-Duse_openmp=false', '-Dgpu_backend=none']
        spaced = mkl('SPACED')
        dirname = os.environ.get('BCI_SPACED_NAME', 'intel mkl')
        if lane == '8a':
            bp = r.cmake_lib('8a.blaspp', 'blaspp', '8a-blaspp', common + ['-DBLAS_LIBRARIES=' + ';'.join(spaced)],
                             checks=[BLAS_MANGLING, defines_has('BLAS_ILP64'), config_has(dirname)])
            r.cmake_lib('8a.lapackpp', 'lapackpp', '8a-lapackpp', ['-Dgpu_backend=none'], deps=[bp],
                        checks=[defines_has('LAPACK_HAVE_LAPACK')])
        else:
            bp = r.cmake_lib('8b.blaspp', 'blaspp', '8b-blaspp', common + ['-DBLAS_LIBRARIES=' + ';'.join(mkl('PLAIN'))],
                             checks=[BLAS_MANGLING, defines_has('BLAS_ILP64')])
            # LAPACK++ found in the BLAS libraries: the MSVC build must keep passing.
            r.cmake_lib('8b.lapackpp', 'lapackpp', '8b-lapackpp', ['-Dgpu_backend=none'], deps=[bp],
                        checks=[defines_has('LAPACK_HAVE_LAPACK')])
            r.cmake_lib('8b.lapackpp.spaced', 'lapackpp', '8b-lapackpp-spaced',
                        ['-Dgpu_backend=none', '-DLAPACK_LIBRARIES=' + ';'.join(spaced)], deps=[bp],
                        checks=[defines_has('LAPACK_HAVE_LAPACK'), config_has(dirname)])
    elif lane == '9':
        lib = system_openblas()
        space, bare = f'{lib} -lm', 'openblas m'
        r.cmake_lib('9.blaspp.space-path', 'blaspp', '9-blaspp-space', lp64 + [f'-DLAPACK_LIBRARIES={space}'],
                    checks=[BLAS_MANGLING, config_has(lib)])
        r.cmake_lib('9.blaspp.bare-names', 'blaspp', '9-blaspp-bare', lp64 + [f'-DLAPACK_LIBRARIES={bare}'],
                    checks=[BLAS_MANGLING])
        bp = r.cmake_lib(None, 'blaspp', '9-blaspp', lp64, consumer=False)
        r.cmake_lib('9.lapackpp.space-path', 'lapackpp', '9-lapackpp-space', [f'-DLAPACK_LIBRARIES={space}'],
                    deps=[bp], checks=[LAPACK_LINUX, config_has(lib)])
        r.cmake_lib('9.lapackpp.bare-names', 'lapackpp', '9-lapackpp-bare', [f'-DLAPACK_LIBRARIES={bare}'],
                    deps=[bp], checks=[LAPACK_LINUX])
        again = lp64 + [f'-DLAPACK_LIBRARIES={space}']
        r.reconfigure('9.blaspp.rerun', 'blaspp', [(again, 'ok'), (again, 'ok', lapack_not_redetected)],
                      again, compare=False)
        r.reconfigure('9.blaspp.rerun-identical', 'blaspp', [(again, 'ok'), (again, 'ok')], again,
                      defines=['BLAS_FORTRAN_*', 'BLAS_HAVE_OPENBLAS'])
        lagain = [f'-DLAPACK_LIBRARIES={space}']
        r.reconfigure('9.lapackpp.rerun', 'lapackpp', [(lagain, 'ok'), (lagain, 'ok', lapack_not_redetected)],
                      lagain, deps=[bp], defines=['LAPACK_HAVE_LAPACK', 'LAPACK_VERSION', 'LAPACK_HAVE_LAPACKE'])
    elif lane == '10':
        lapack_suffix_lane(r)
    else:
        raise RuntimeError(f'no recipe for lane {lane}')


def lib_file(name):
    found = subprocess.run(['gcc', f'-print-file-name={name}'], capture_output=True, text=True).stdout.strip()
    if not os.path.isabs(found):
        raise RuntimeError(f'{name} not found')
    return os.path.realpath(found)


def lapack_suffix_lane(r):
    """Lane 10. BLAS++ (static) gets the reference BLAS and, because it needs
    a few LAPACK routines (and probes pstrf), only those archive members of
    the reference LAPACK, unsuffixed: no ilaver_ among them. LAPACK++ (static)
    gets the reference LAPACK with every symbol it defines renamed <name>x_
    (dpstrf_x_, ilaver_x_, ...) and lapack_symbol_suffix=x_. It must find
    that library (defines.h has LAPACK_FORTRAN_SUFFIX) and its version
    (LAPACK_VERSION, probed through ilaver_x_)."""
    work = r.work / 'lapack-suffix'
    shutil.rmtree(work, ignore_errors=True)
    (work / 'min').mkdir(parents=True)
    lapack_a, blas_so = lib_file('liblapack.a'), lib_file('libblas.so')
    log = r.logs / '10-libraries.log'
    r.group('lane 10: suffixed and minimal LAPACK archives')
    nm = subprocess.run(['nm', '--defined-only', '-g', lapack_a], capture_output=True, text=True).stdout
    syms = sorted({ln.split()[-1] for ln in nm.splitlines() if len(ln.split()) == 3})
    (work / 'rename.txt').write_text(''.join(f'{x} {x}x_\n' for x in syms))
    suffixed = work / 'liblapack_x.a'
    if run(['objcopy', f'--redefine-syms={work / "rename.txt"}', lapack_a, suffixed], log) != 0:
        raise RuntimeError('objcopy failed')
    needed = ['dpstrf_', 'crot_', 'zrot_', 'csymv_', 'zsymv_', 'csyr_', 'zsyr_']
    (work / 'probe.c').write_text(''.join(f'extern void {n}(void);\n' for n in needed)
                                  + 'int main(void) {\n' + ''.join(f'    {n}();\n' for n in needed) + '    return 0;\n}\n')
    # The link map lists every archive member the link pulled in, as
    # "<archive>(<member>)".
    trace = subprocess.run(['gcc', str(work / 'probe.c'), '-o', str(work / 'probe'),
                            f'-Wl,-Map={work / "probe.map"}', lapack_a, blas_so, '-lgfortran', '-lm'],
                           capture_output=True, text=True)
    linkmap = (work / 'probe.map').read_text(errors='replace') if (work / 'probe.map').exists() else ''
    members = sorted(set(re.findall(re.escape(lapack_a) + r'\((\S+?\.o)\)', linkmap)))
    if trace.returncode != 0 or not members:
        raise RuntimeError(f'cannot pick the LAPACK members BLAS++ needs (link exit {trace.returncode}): '
                           f'{trace.stderr[-500:]}')
    run(['ar', 'x', lapack_a, *members], log, cwd=work / 'min')
    minimal = work / 'liblapack_min.a'
    run(['ar', 'rcs', minimal, *members], log, cwd=work / 'min')
    have = lambda a: set(subprocess.run(['nm', '--defined-only', '-g', str(a)], capture_output=True,
                                        text=True).stdout.split())
    hx, hm = have(suffixed), have(minimal)
    sane = {'dpstrf_x_', 'ilaver_x_'} <= hx and 'dpstrf_' not in hx and 'dpstrf_' in hm and 'ilaver_' not in hm
    print(f'{len(syms)} LAPACK symbols renamed <name>x_; BLAS++ gets {len(members)} unsuffixed members '
          f'({", ".join(members[:8])}{", ..." if len(members) > 8 else ""}); sane: {sane}')
    r.endgroup()
    if not sane:
        raise RuntimeError('the suffixed or minimal LAPACK archive is not as intended')
    static = ['-DBUILD_SHARED_LIBS=OFF', '-Dgpu_backend=none']
    bp = r.cmake_lib(None, 'blaspp', '10-blaspp', static + [
        f'-DBLAS_LIBRARIES={blas_so}', f'-DLAPACK_LIBRARIES={minimal};-lgfortran;-lm', '-Dblas_int=int32'],
        consumer=False)
    k1, k2 = '10.lapackpp.configure', '10.lapackpp.defines'
    if bp is None:
        r.blocked(k1, ['configure'], 'BLAS++ build failed')
        r.blocked(k2, ['version'], 'BLAS++ build failed')
        return
    tree = r.work / 'build' / '10-lapackpp'
    shutil.rmtree(tree, ignore_errors=True)
    r.group(f'{k1}: LAPACK++ with lapack_symbol_suffix=x_')
    t0 = time.time()
    rc = r.sh(['cmake', '-S', r.src['lapackpp'], '-B', tree, *r.gen, '-DCMAKE_BUILD_TYPE=Release', '-Dcolor=no',
               '-Dbuild_tests=no', *static, '-Dlapack_symbol_suffix=x_',
               f'-DLAPACK_LIBRARIES={suffixed};-lgfortran;-lm', f'-DCMAKE_PREFIX_PATH={bp}'],
              f'{k1}.configure', echo=True)
    text = (r.logs / f'{k1}.configure.log').read_text(encoding='utf-8', errors='replace')
    why = ''
    if re.search(r'not used by the project:\s*\n\s*lapack_symbol_suffix', text):
        why = 'this LAPACK++ has no lapack_symbol_suffix option (L2 is not in main)'
    r.endgroup()
    r.phase(k1, 'configure', rc == 0, why or ('' if rc == 0 else 'configure failed'), t0)
    if rc != 0:
        r.blocked(k2, ['version'], 'configure failed' + (f': {why}' if why else ''))
        return
    defines = (tree / FILES['lapackpp'][0]).read_text(encoding='utf-8', errors='replace')
    has = lambda n: re.search(r'#define\s+' + n + r'\b', defines) is not None
    r.phase(k1, 'suffixed', has('LAPACK_FORTRAN_SUFFIX'),
            '' if has('LAPACK_FORTRAN_SUFFIX') else
            'defines.h has no LAPACK_FORTRAN_SUFFIX: LAPACK was not found as the suffixed library'
            + (f' ({why})' if why else ''))
    r.phase(k2, 'version', has('LAPACK_VERSION'),
            '' if has('LAPACK_VERSION') else 'defines.h has no LAPACK_VERSION: the version probe did not use '
            'the LAPACK suffix' + (f' ({why})' if why else ''))
    t0 = time.time()
    ok = r.sh(['cmake', '--build', tree, '--target', 'lapackpp', '--parallel', r.jobs], f'{k1}.build') == 0
    if not ok:
        print(tail(r.logs / f'{k1}.build.log', 60))
    r.phase(k1, 'build', ok, '' if ok else 'LAPACK++ library build failed', t0)


def zero_dims(r, key, built, test_dir):
    """gemqrt with m = 0 (side R) and n = 0 (side L): lwork is 0 there, and
    &work[0] on an empty std::vector trips _GLIBCXX_ASSERTIONS. With
    --align 32, ldv = 32 is valid for both shapes. Upstream's tester also takes
    &C[0] of its empty C when n = 0, so the side-L runs need L3's tester fix
    as well as its library fix."""
    exe = test_dir / 'tester'
    if built is None or not exe.exists():
        r.blocked(key, ['zero-dims'], 'assertions build or tester missing')
        return
    r.group(f'{key}: gemqrt with zero dimensions')
    t0, bad = time.time(), []
    runs = [
        ['--type', 's,d', '--side', 'r', '--trans', 'n,t,c', '--dim', '0x10x5'],
        ['--type', 'c,z', '--side', 'r', '--trans', 'n,c', '--dim', '0x10x5'],
        ['--type', 's,d', '--side', 'l', '--trans', 'n,t,c', '--dim', '10x0x5'],
        ['--type', 'c,z', '--side', 'l', '--trans', 'n,c', '--dim', '10x0x5'],
    ]
    for i, args in enumerate(runs):
        log = r.logs / f'{key}.run{i}.log'
        rc = r.sh(['./tester', '--align', '32', *args, 'gemqrt'], f'{key}.run{i}', cwd=test_dir,
                  echo=True, timeout=600)
        text = log.read_text(encoding='utf-8', errors='replace') if log.exists() else ''
        rows = [ln for ln in text.splitlines() if ln.split()[-1:] == ['pass']]
        if rc != 0:
            bad.append(f'{" ".join(args)} -> exit {rc}')
        elif 'All tests passed for gemqrt.' not in text or not rows:
            bad.append(f'{" ".join(args)} -> exit 0, but no passing gemqrt results were printed')
    r.endgroup()
    r.phase(key, 'zero-dims', not bad, '; '.join(bad), t0)


def lane6(r):
    blas = ['-Dblas=openblas', '-Dblas_int=int32']

    def build_lapackpp_against(tree, prefix):
        key = '6.blaspp.identical'
        r.group(f'{key}: LAPACK++ against the re-configured BLAS++')
        t0 = time.time()
        ldir = r.work / 'build' / f'{key}.lapackpp'
        shutil.rmtree(ldir, ignore_errors=True)
        ok = r.sh(['cmake', '--build', tree, '--target', 'blaspp', '--parallel', r.jobs], f'{key}.consumer') == 0
        ok = ok and r.sh(['cmake', '--install', tree], f'{key}.consumer') == 0
        ok = ok and r.sh(['cmake', '-S', r.src['lapackpp'], '-B', ldir, *r.gen, '-DCMAKE_BUILD_TYPE=Release',
                          '-Dcolor=no', '-Dbuild_tests=no', '-Dgpu_backend=none',
                          f'-DCMAKE_PREFIX_PATH={prefix}'], f'{key}.consumer', echo=True) == 0
        ok = ok and r.sh(['cmake', '--build', ldir, '--target', 'lapackpp', '--parallel', r.jobs],
                         f'{key}.consumer') == 0
        if not ok:
            print(tail(r.logs / f'{key}.consumer.log', 60))
        r.endgroup()
        r.phase(key, 'lapackpp-build', ok, '' if ok else 'LAPACK++ did not configure or build against it', t0)

    # BLAS++: identical re-run; changed blas_fortran; suffix added after a
    # failed configure; library that appears after a failed configure.
    good = ['BLAS_FORTRAN_*', 'BLAS_HAVE_OPENBLAS']
    r.reconfigure('6.blaspp.identical', 'blaspp', [(blas, 'ok'), (blas, 'ok')], blas,
                  after=build_lapackpp_against, defines=good)
    r.reconfigure('6.blaspp.fortran', 'blaspp', [(blas, 'ok'), (blas + ['-Dblas_fortran=gfortran'], 'ok')],
                  blas + ['-Dblas_fortran=gfortran'], defines=good)
    conda = os.environ.get('CONDA_PREFIX')
    if conda:
        env = dict(r.env)
        env['LD_LIBRARY_PATH'] = os.pathsep.join([f'{conda}/lib'] + ([env['LD_LIBRARY_PATH']] if env.get('LD_LIBRARY_PATH') else []))
        sfx = [f'-DBLAS_LIBRARIES={conda}/lib/libopenblas64_.so', '-Dblas_int=int64']
        r.reconfigure('6.blaspp.suffix', 'blaspp', [(sfx, 'fail'), (sfx + ['-Dblas_symbol_suffix=64_'], 'ok')],
                      sfx + ['-Dblas_symbol_suffix=64_'], env=env,
                      defines=['BLAS_FORTRAN_*', 'BLAS_ILP64', 'BLAS_FORTRAN_SUFFIX'])
    else:
        r.blocked('6.blaspp.suffix', ['reconfigure'], 'CONDA_PREFIX not set (conda OpenBLAS missing)')
    late = r.work / 'late-blas'
    shutil.rmtree(late, ignore_errors=True)
    late_lib = ['-DBLAS_LIBRARIES=' + str(late / 'libopenblas.so')]
    r.reconfigure('6.blaspp.failed-then-fixed', 'blaspp',
                  [(late_lib, 'fail'), lambda: copy_openblas(late), (late_lib, 'ok')], late_lib, defines=good)

    # LAPACK++, against a fresh BLAS++: identical re-run; changed `lapack`
    # option; LAPACK_LIBRARIES that appears after a failed configure.
    bp = r.cmake_lib(None, 'blaspp', '6-blaspp', blas, consumer=False)
    lgood = ['LAPACK_HAVE_LAPACK', 'LAPACK_VERSION', 'LAPACK_HAVE_LAPACKE']
    r.reconfigure('6.lapackpp.identical', 'lapackpp', [([], 'ok'), ([], 'ok')], [], deps=[bp], defines=lgood)
    r.reconfigure('6.lapackpp.option', 'lapackpp', [([], 'ok'), (['-Dlapack=default'], 'ok')],
                  ['-Dlapack=default'], deps=[bp], defines=lgood)
    late2 = r.work / 'late-lapack'
    shutil.rmtree(late2, ignore_errors=True)
    late_lapack = ['-DLAPACK_LIBRARIES=' + str(late2 / 'libopenblas.so')]
    r.reconfigure('6.lapackpp.failed-then-fixed', 'lapackpp',
                  [(late_lapack, 'fail'), lambda: copy_openblas(late2), (late_lapack, 'ok')], late_lapack,
                  deps=[bp], defines=lgood)

    # BLAS++ with LAPACK as a separate -llapack (lapack=generic), configured and
    # then re-configured three times unchanged: the exported blaspp_libraries
    # must match a fresh configure after every run.
    generic = blas + ['-Dlapack=generic']
    r.reconfigure('6.blaspp.lapack-generic', 'blaspp', [(generic, 'ok')] * 4, generic,
                  only=('blaspp_libraries',))

    # LAPACK++ on CMake's FindLAPACK path (BLA_VENDOR=OpenBLAS finds the LP64
    # libopenblas), configured against an ILP64 BLAS++ install
    # (libopenblas64, named explicitly: an LP64 library passes BLASFinder's
    # ILP64 probe); BLAS++ is then re-installed into the same prefix as LP64,
    # and LAPACK++'s re-configure must match a fresh configure against it.
    swap = r.work / 'install' / '6-swap-blaspp'
    shutil.rmtree(swap, ignore_errors=True)

    def install_blaspp(name, args):
        tree = r.work / 'build' / name
        shutil.rmtree(tree, ignore_errors=True)
        cmds = [['cmake', '-S', r.src['blaspp'], '-B', tree, *r.gen, '-DCMAKE_BUILD_TYPE=Release', '-Dcolor=no',
                 '-Dbuild_tests=no', '-Dgpu_backend=none', f'-DCMAKE_INSTALL_PREFIX={swap}', *args],
                ['cmake', '--build', tree, '--target', 'blaspp', '--parallel', r.jobs],
                ['cmake', '--install', tree]]
        for c in cmds:
            if r.sh(c, f'6.lapackpp.findlapack-blaspp-swap.{name}', echo=c[1] == '-S') != 0:
                return f'BLAS++ install {name} failed'
        return None

    first = install_blaspp('blaspp-int64', ['-DBLAS_LIBRARIES=-lopenblas64', '-Dblas_int=int64'])
    if first:
        r.blocked('6.lapackpp.findlapack-blaspp-swap', ['reconfigure'], first)
    else:
        found = ['-DBLA_VENDOR=OpenBLAS']
        r.reconfigure('6.lapackpp.findlapack-blaspp-swap', 'lapackpp',
                      [(found, 'ok'), lambda: install_blaspp('blaspp-int32', blas), (found, 'ok')], found,
                      deps=[swap], defines=['LAPACK_VERSION', 'LAPACK_HAVE_LAPACKE'])


def copy_openblas(dest):
    found = subprocess.run(['gcc', '-print-file-name=libopenblas.so'], capture_output=True, text=True).stdout.strip()
    dest.mkdir(parents=True, exist_ok=True)
    real = os.path.realpath(found)
    for name in ('libopenblas.so', 'libopenblas.so.0'):
        shutil.copy2(real, dest / name)
    print(f'copied {real} into {dest}')


# ----------------------------------------------------------------- judging

def parse_expectations(text, where):
    exp, errors = collections.OrderedDict(), []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        m = re.match(r'^([A-Za-z0-9_.-]+)\s*:\s*(pass|xfail:([A-Za-z0-9]+))$', line)
        if not m:
            errors.append(f'{where}:{n}: cannot parse {raw.strip()!r}')
            continue
        if m.group(1) in exp:
            errors.append(f'{where}:{n}: {m.group(1)} listed twice')
        exp[m.group(1)] = m.group(2)
    return exp, errors


def validate_expectations(exp, repo, where):
    """The file of `repo` must list exactly the sub-checks it owns, with
    `xfail:<owner>` only for a sub-check owned by that patch."""
    errors = []
    want = [sc for sc in SUB if repo_of(sc) == repo]
    for sc in want:
        if sc not in exp:
            errors.append(f'{where}: no entry for {sc}')
    for sc, value in exp.items():
        if sc not in SUB:
            errors.append(f'{where}: {sc} is not a sub-check in ci.py')
        elif repo_of(sc) != repo:
            errors.append(f'{where}: {sc} belongs in BallisticLA/{repo_of(sc)}\'s file')
        elif value != 'pass':
            owner = value.split(':', 1)[1]
            if owner != SUB[sc]['owner']:
                errors.append(f'{where}: {sc} is {value}, but its owning patch is '
                              f'{SUB[sc]["owner"] or "none (it must pass)"}')
    return errors


def workflow_lib():
    """The library of the repository running the workflow; None outside Actions."""
    name = os.environ.get('GITHUB_REPOSITORY', '').split('/')[-1]
    return name if name in FORK else None


def default_expectation(sc):
    """The expectation for unmodified upstream: owned sub-checks fail."""
    return f'xfail:{SUB[sc]["owner"]}' if SUB[sc]['owner'] else 'pass'


def load_expectations(mode, srcs, strict=None):
    """Expected outcome per sub-check: 'pass' or 'xfail:<patch>'. srcs maps
    library -> source tree of the commit under test. The file of each fork in
    `strict` (default: the fork running the workflow, or both outside
    Actions) must be exactly right. The other fork's file has passed that
    fork's own runs; problems in it (a ci.py that differs between the forks
    while a CI change lands in one of them first) become notices, and the
    affected entries fall back to their upstream defaults."""
    exp, errors, notes = {}, [], []
    if mode == 'candidate':
        return {sc: 'pass' for sc in SUB}, [], ['expect=candidate: every sub-check must pass']
    if mode == 'baseline':
        return ({sc: default_expectation(sc) for sc in SUB}, [],
                ['expect=baseline: owned sub-checks must fail, the rest pass'])
    if strict is None:
        strict = {workflow_lib()} if workflow_lib() else {'blaspp', 'lapackpp'}
    for repo in ('blaspp', 'lapackpp'):
        path = Path(srcs[repo]) / EXPECTATIONS
        where = f'BallisticLA/{repo}:{EXPECTATIONS}'
        mine = [sc for sc in SUB if repo_of(sc) == repo]
        if not path.exists():
            notes.append(f'{where} does not exist at the commit under test; treating that commit as '
                         'unmodified upstream (its owned sub-checks must fail, the rest pass)')
            exp.update({sc: default_expectation(sc) for sc in mine})
            continue
        parsed, errs = parse_expectations(path.read_text(encoding='utf-8'), where)
        errs += validate_expectations(parsed, repo, where)
        if repo in strict:
            errors += errs
            exp.update(parsed)
            continue
        notes += [f'{e} (other fork: entry falls back to its default)' for e in errs]
        for sc in mine:
            v = parsed.get(sc)
            if v is None or (v != 'pass' and v.split(':', 1)[1] != SUB[sc]['owner']):
                v = default_expectation(sc)
            exp[sc] = v
    return exp, errors, notes


CI_FILES = ('.github/ballistic/ci.py', '.github/workflows/ballistic-ci.yml')
# Every file under these directories is CI code: adding, removing or changing
# one needs the label (a module next to ci.py, or a second workflow with a job
# named like the gate, would change what the gate does).
CI_DIRS = ('.github/ballistic', '.github/workflows')


def rules_loosened(base_src, head_src):
    """Changes from a pull request's base to its head that relax the gate in
    the fork under test. Returns (loosenings, notes). Counted as relaxing,
    once the base has F0:
    - any added, removed or changed file under .github/ballistic/ or
      .github/workflows/ (the CI code itself);
    - an expectation that leaves `pass` or disappears;
    - a baseline file or routine that disappears; a baseline routine that no
      longer passes, fails in more rows, or prints fewer result rows;
    - a routine added to an existing baseline with a failing result;
    - a new baseline file that records any failing routine, or that misses a
      routine (or its result rows) of the baseline it replaces as reference:
      REFERENCE[sc] at the base, else 1-cmake.<lib> at the base;
    - a baseline recorded with other tester arguments than the sub-check's
      (TESTER_ARGS), or with an invalid result-row count.
    Flipping an xfail to pass, and adding passing routines, tighten the gate."""
    out, notes = [], []
    bpath, hpath = Path(base_src) / EXPECTATIONS, Path(head_src) / EXPECTATIONS
    if not bpath.exists():
        notes.append('the base commit has no F0 (no expectations file): no gate to relax yet')
        return out, notes

    def ci_files(root):
        files = {}
        for d in CI_DIRS:
            top = Path(root) / d
            for f in sorted(top.rglob('*')) if top.is_dir() else []:
                if f.is_file():
                    files[f.relative_to(root).as_posix()] = f.read_bytes()
        return files

    bci, hci = ci_files(base_src), ci_files(head_src)
    for name in sorted(set(bci) | set(hci)):
        if bci.get(name) != hci.get(name):
            what = 'added' if name not in bci else 'removed' if name not in hci else 'changed'
            out.append(f'{name} {what} (CI code: the gate\'s own code)')
    bexp, _ = parse_expectations(bpath.read_text(encoding='utf-8'), 'base')
    hexp, _ = parse_expectations(hpath.read_text(encoding='utf-8') if hpath.exists() else '', 'head')
    for sc, b in bexp.items():
        h = hexp.get(sc)
        if h is None:
            out.append(f'{EXPECTATIONS}: {sc} removed (was {b})')
        elif b == 'pass' and h != 'pass':
            out.append(f'{EXPECTATIONS}: {sc} {b} -> {h}')

    def load(d, name):
        f = Path(d) / BASELINE_DIR / name
        return json.loads(f.read_text(encoding='utf-8')) if f.exists() else None

    def names(d):
        p = Path(d) / BASELINE_DIR
        return {f.name for f in p.glob('*.json')} if p.is_dir() else set()

    for name in sorted(names(base_src) | names(head_src)):
        b, h = load(base_src, name), load(head_src, name)
        if h is None:
            out.append(f'{BASELINE_DIR}/{name} removed')
            continue
        hr, hd = h.get('routines', {}), h.get('details', {})
        sc = name[:-len('.json')]
        if sc in SUB and SUB[sc]['tester'] == GATE and h.get('args') != tester_args(sc):
            out.append(f'{name}: recorded with tester arguments {h.get("args")}, not {tester_args(sc)}')
        if bad_counts(hd):
            out.append(f'{name}: invalid result-row counts for {", ".join(bad_counts(hd)[:6])}')
        if b is not None:
            br, bd = b.get('routines', {}), b.get('details', {})
            for k, bv in br.items():
                hv = hr.get(k)
                if hv is None:
                    out.append(f'{name}: {k} removed')
                    continue
                if bv == 'pass' and hv != 'pass':
                    out.append(f'{name}: {k} pass -> {hv}')
                elif bv != 'pass' and hv != 'pass':
                    extra = set(hd.get(k, {}).get('rows', [])) - set(bd.get(k, {}).get('rows', []))
                    if extra:
                        out.append(f'{name}: {k} fails in more rows: {", ".join(sorted(extra))}')
                bn, hn = rows_of(bd, k), rows_of(hd, k)
                if bn is not None and (hn is None or hn < bn):
                    out.append(f'{name}: {k} result rows {bn} -> {hn}')
            for k, hv in hr.items():
                if k not in br and hv != 'pass':
                    out.append(f'{name}: {k} added as {hv}')
            if b.get('exports') is not None and h.get('exports') != b.get('exports'):
                out.append(f'{name}: exported libraries/defines changed')
            continue
        # A new baseline replaces the reference the gate used until now.
        for k, hv in hr.items():
            if hv != 'pass':
                out.append(f'{name} (new): {k} recorded as {hv}')
        if sc not in SUB:
            out.append(f'{name} (new): not a sub-check in ci.py')
            continue
        if tester_args(sc) != default_tests(SUB[sc]['lib']):
            continue
        ref_name = REFERENCE.get(sc, f'1-cmake.{SUB[sc]["lib"]}') + '.json'
        ref = load(base_src, ref_name)
        if ref is None:
            continue
        missing = [k for k in ref['routines'] if k not in hr]
        if missing:
            out.append(f'{name} (new): lacks {len(missing)} routine(s) of {ref_name}: {", ".join(missing[:6])}')
        thin = [k for k in ref['routines'] if k in hr
                and (rows_of(ref.get('details'), k) or 0) > 0 and not rows_of(hd, k)]
        if thin:
            out.append(f'{name} (new): no result rows for {", ".join(thin[:6])}')
    return out, notes


def load_baseline(srcs, sc):
    """The stored baseline of sub-check sc ({'routines': ..., 'details': ...}), or None."""
    path = Path(srcs[SUB[sc]['lib']]) / BASELINE_DIR / f'{sc}.json'
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def default_tests(lib):
    return BLASPP_TESTS if lib == 'blaspp' else LAPACKPP_TESTS


# run_tests.py arguments of each sub-check that runs a tester; the lanes use
# these, and a baseline recorded with other arguments is rejected.
TESTER_ARGS = {'6b.lapackpp.sanity': ['--quick', 'gemqrt']}


def tester_args(sc):
    return list(TESTER_ARGS.get(sc, default_tests(SUB[sc]['lib'])))


def tester_args_of(sc):
    return tester_args(sc)


def valid_n(v):
    """A result-row count: a non-negative int (not a bool, NaN or float)."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def bad_counts(details):
    """Routines whose recorded result-row count is not a valid count."""
    return [k for k, d in (details or {}).items() if 'n' in d and not valid_n(d['n'])]


def reference_routines(srcs, sc, args):
    """The baseline a sub-check without its own must still cover: 1-cmake.<lib>'s,
    when the sub-check ran the same run_tests.py arguments."""
    lib = SUB[sc]['lib']
    if args != default_tests(lib):
        return None
    return load_baseline(srcs, f'1-cmake.{lib}')


def rows_of(details, k):
    """Result rows a routine printed, or None if not recorded."""
    return (details or {}).get(k, {}).get('n')


def compare_routines(cur, base, details=None, reference=None):
    """Problems (fail the sub-check) and notes, comparing per-routine results
    with the stored baseline.

    With a baseline: a routine that passed must still run and pass, and print
    at least as many result rows (a routine compiled out of the tester prints
    none and still passes); a new routine must pass; a routine that failed may
    keep failing, but only in the result rows that failed there and with an
    exit status equal to its number of failed rows. Without one: every routine
    must pass, and every routine of `reference` (1-cmake.<lib>'s baseline)
    must have run and printed result rows if it printed any there."""
    problems, notes, details = [], [], details or {}
    if base is None:
        failed = [k for k, v in cur.items() if v != 'pass']
        if failed:
            problems.append(f'{len(failed)} routine(s) failed (no stored baseline): ' + ', '.join(failed[:8]))
        if reference:
            missing = [k for k in reference['routines'] if k not in cur]
            if missing:
                problems.append(f'{len(missing)} routine(s) of the 1-cmake reference did not run: '
                                + ', '.join(missing[:8]))
            empty = [k for k in reference['routines'] if k in cur and rows_of(details, k) == 0
                     and (rows_of(reference.get('details'), k) or 0) > 0]
            if empty:
                problems.append(f'{len(empty)} routine(s) printed no result rows (compiled out?): '
                                + ', '.join(empty[:8]))
        return problems, notes
    broutines, bdetails = base['routines'], base.get('details', {})
    if bad_counts(bdetails):
        problems.append('baseline has invalid result-row counts: ' + ', '.join(bad_counts(bdetails)[:6]))
    for k, b in broutines.items():
        c = cur.get(k)
        bn, cn = rows_of(bdetails, k), rows_of(details, k)
        if c is not None and bn is not None and (cn is None or cn < bn):
            problems.append(f'{k}: printed {cn} result rows, baseline {bn}')
        if c is None:
            problems.append(f'{k}: did not run (baseline {b})')
        elif b == 'pass' and c != 'pass':
            problems.append(f'{k}: regressed (baseline pass, now {c})')
        elif b != 'pass' and c == 'pass':
            notes.append(f'{k}: now passes (baseline {b})')
        elif b != 'pass':
            cd, bd = details.get(k, {}), bdetails.get(k)
            rows = cd.get('rows', [])
            if bd is None or 'rows' not in bd:
                problems.append(f'{k}: known failure without recorded rows in the baseline')
            elif set(rows) - set(bd['rows']):
                problems.append(f'{k}: known failure, but new failed rows: '
                                + ', '.join(sorted(set(rows) - set(bd['rows']))[:6]))
            elif cd.get('exit') != len(rows) or not rows:
                problems.append(f'{k}: exit status {cd.get("exit")} with {len(rows)} failed rows '
                                '(a crash or an unreported failure)')
            else:
                notes.append(f'{k}: known failure ({", ".join(rows)})')
    for k, c in cur.items():
        if k not in broutines:
            if c != 'pass':
                problems.append(f'{k}: fails (not in baseline)')
            else:
                notes.append(f'{k}: new routine, passes')
    return problems, notes


def outcome(sc, rec, srcs):
    """('pass' | 'fail', reasons, notes) for one sub-check's recorded results."""
    reasons, notes = [], []
    if not rec['phases']:
        reasons.append('no phases recorded')
    for name, ph in rec['phases'].items():
        if ph['status'] != 'pass':
            reasons.append(f'{name}: {ph["status"]}' + (f' ({ph["note"]})' if ph.get('note') else ''))
    own = load_baseline(srcs, sc)
    if own and own.get('exports') is not None:
        for var, want in own['exports'].items():
            got = (rec.get('exports') or {}).get(var)
            if got != want:
                reasons.append(f'exports: {var} is {got!r}; the baseline exports {want!r}')
    mode, t = SUB[sc]['tester'], rec.get('tester')
    if mode == GATE:
        if not t or not t.get('present'):
            reasons.append('tester: expected but not built')
        elif t.get('parse_error'):
            reasons.append(f'tester: {t["parse_error"]}')
        else:
            base, required, lib = load_baseline(srcs, sc), None, SUB[sc]['lib']
            if base is None and sc in REFERENCE and t.get('args') == default_tests(lib):
                base = load_baseline(srcs, REFERENCE[sc])
                if base is not None:
                    notes.append(f'compared with the {REFERENCE[sc]} baseline (same BLAS library)')
            if base is None:
                required = reference_routines(srcs, sc, t.get('args'))
            problems, n = compare_routines(t['routines'], base, t.get('details'), reference=required)
            if base is not None and base.get('args') is not None and base.get('args') != t.get('args'):
                problems.append(f'the baseline was recorded with tester arguments {base.get("args")}, '
                                f'this run used {t.get("args")}')
            if problems and base is None and not (Path(srcs[lib]) / EXPECTATIONS).exists():
                # That fork's commit is unmodified upstream (no F0): there is no
                # baseline to hold its known failures to, so record them.
                notes += [f'tester (recorded; BallisticLA/{lib} has no F0 baselines at this commit): {p}'
                          for p in problems]
                problems = []
            reasons += [f'tester: {p}' for p in problems]
            notes += n
    elif mode == RECORD and t:
        if not t.get('present'):
            notes.append('tester (best effort): not built')
        else:
            failed = [k for k, v in t.get('routines', {}).items() if v != 'pass']
            notes.append(f'tester (best effort): {len(t.get("routines", {}))} routines, {len(failed)} failed'
                         + (f', {t["parse_error"]}' if t.get('parse_error') else ''))
    return ('fail' if reasons else 'pass'), reasons, notes


def judge(results, lanes, mode, srcs, shas, strict=None):
    """Compare recorded results of `lanes` with the expectations. Returns
    (rows, ok)."""
    exp, errors, notes = load_expectations(mode, srcs, strict)
    for n in notes:
        print(f'::notice::{n}')
    for e in errors:
        print(f'::error::{e}')
    rows, ok = [], not errors
    for lane in lanes:
        res = results.get(lane)
        problem = None
        if res is None:
            problem = 'no results (lane did not run or its artifact is missing)'
        elif res.get('infra') != 'ok':
            problem = f'lane infrastructure failed: {res.get("infra")}'
        elif any(res[lib]['sha'] != shas[lib] for lib in ('blaspp', 'lapackpp')):
            problem = 'results are for different commits than the ones under test'
        for sc, _, _ in LANES[lane][1]:
            want = exp.get(sc, 'pass')
            if problem:
                rows.append((sc, want, 'missing', 'MISMATCH', [problem], []))
                ok = False
                continue
            rec = res['subchecks'].get(sc)
            if rec is None:
                rows.append((sc, want, 'missing', 'MISMATCH', ['sub-check not recorded'], []))
                ok = False
                continue
            got, reasons, nts = outcome(sc, rec, srcs)
            if want == 'pass' and got == 'fail':
                verdict, good = 'MISMATCH: unexpected failure', False
            elif want != 'pass' and got == 'pass':
                verdict, good = (f'MISMATCH: unexpected pass ({want.split(":")[1]} looks fixed; '
                                 'flip this entry to pass)'), False
            else:
                verdict, good = ('ok' if got == 'pass' else 'ok (expected failure)'), True
            ok = ok and good
            rows.append((sc, want, got, verdict, reasons, nts))
    return rows, ok


def report(rows, ok, title):
    print(f'\n{title}')
    w = max([len(r[0]) for r in rows] + [10])
    for sc, want, got, verdict, reasons, nts in rows:
        print(f'  {sc:<{w}}  expect {want:<10}  got {got:<7}  {verdict}')
        for x in reasons:
            print(f'  {"":<{w}}    - {x}')
        for x in nts:
            print(f'  {"":<{w}}    . {x}')
    print('RESULT: ' + ('every sub-check matches its expectation' if ok else 'MISMATCH (see above)'))
    md = [f'### {title}', '', '| sub-check | owner | expected | outcome | verdict |', '|---|---|---|---|---|']
    for sc, want, got, verdict, reasons, nts in rows:
        detail = '<br>'.join([verdict] + [r.replace('|', '\\|') for r in reasons[:4]])
        md.append(f'| `{sc}` | {SUB[sc]["owner"] or "-"} | {want} | {got} | {detail} |')
    md.append('')
    md.append('**' + ('All sub-checks match their expectations.' if ok else 'Mismatch: this run fails.') + '**')
    summary('\n'.join(md))


# ---------------------------------------------------------------- commands

def cmd_resolve(a):
    repo = os.environ['GITHUB_REPOSITORY']
    name = repo.split('/')[1]
    if name not in FORK:
        die(f'unexpected repository {repo}: the workflow expects a blaspp or lapackpp fork')
    event = os.environ['GITHUB_EVENT_NAME']
    server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com')
    url = {name: f'{server}/{repo}.git', other(name): FORK[other(name)]}
    sha, how = {}, {}
    if event == 'workflow_dispatch':
        for lib in ('blaspp', 'lapackpp'):
            given = os.environ.get(f'INPUT_{lib.upper()}_REF', '').strip()
            if given:
                if not SHA_RE.match(given):
                    die(f'{lib}_ref must be a full 40-character commit SHA, got {given!r}')
                sha[lib], how[lib] = given, 'workflow_dispatch input'
            elif lib == name:
                # Empty for this fork: the commit the run was started from.
                sha[lib], how[lib] = os.environ['GITHUB_SHA'], 'the dispatched commit'
            else:
                sha[lib], how[lib] = ls_remote_main(url[lib]), 'main (resolved now)'
        expect = os.environ.get('INPUT_EXPECT', '').strip() or 'file'
    else:
        sha[name], how[name] = os.environ['GITHUB_SHA'], f'{event} commit'
        sha[other(name)], how[other(name)] = ls_remote_main(url[other(name)]), 'main (resolved now)'
        expect = 'file'
    base_sha, label = '', 'false'
    if event == 'pull_request':
        ev = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        base_sha = ev['pull_request']['base']['sha']
        if any(lb.get('name') == RULES_LABEL for lb in ev['pull_request'].get('labels', [])):
            label = 'true'
    if expect not in ('file', 'candidate', 'baseline'):
        die(f'expect must be file, candidate or baseline, got {expect!r}')
    lines = [f'{lib}: {url[lib]} @ {sha[lib]}  [{how[lib]}]' for lib in ('blaspp', 'lapackpp')]
    lines.append(f'expectations: {expect}')
    if base_sha:
        lines.append(f'pull request base: {base_sha}; label {RULES_LABEL}: {label}')
    print('\n'.join(lines))
    summary('### Commits under test\n\n' + '\n'.join(f'- {x}' for x in lines))
    for lib in ('blaspp', 'lapackpp'):
        gh_out(f'{lib}_url', url[lib])
        gh_out(f'{lib}_sha', sha[lib])
    gh_out('expect', expect)
    gh_out('base_sha', base_sha)
    gh_out('rules_label', label)


def cmd_lane(a):
    if a.lane not in LANES:
        die(f'unknown lane {a.lane}')
    r = Runner(a.lane, a)
    t0 = time.time()
    try:
        r.fetch_sources()
        recipe(r)
        r.res['infra'] = 'ok'
    except Exception as e:
        import traceback
        traceback.print_exc()
        r.res['infra'] = f'error: {e!r}'
    r.res['seconds'] = round(time.time() - t0)
    r.write()
    srcs = {'blaspp': r.src['blaspp'], 'lapackpp': r.src['lapackpp']}
    shas = {lib: r.res[lib]['sha'] for lib in ('blaspp', 'lapackpp')}
    rows, ok = judge({a.lane: r.res}, [a.lane], a.expect, srcs, shas)
    report(rows, ok, f'Lane {a.lane}')
    sys.exit(0 if ok else 1)


def load_results(results_dir):
    results = {}
    for f in sorted(Path(results_dir).rglob('result.json')):
        res = json.loads(f.read_text(encoding='utf-8'))
        if res['lane'] in results:
            die(f'two result files for lane {res["lane"]}')
        results[res['lane']] = res
    return results


def cmd_check(a):
    work = Path(a.work).resolve()
    srcs = {lib: work / 'src' / lib for lib in ('blaspp', 'lapackpp')}
    shas = {'blaspp': a.blaspp_sha, 'lapackpp': a.lapackpp_sha}
    for lib, url in (('blaspp', a.blaspp_url), ('lapackpp', a.lapackpp_url)):
        fetch(url, shas[lib], srcs[lib], work / 'fetch.log')
    results = load_results(a.results)
    for lane in results:
        if lane not in LANES:
            print(f'::error::results for unknown lane {lane}')
    copies = {lib: srcs[lib] / '.github' / 'ballistic' / 'ci.py' for lib in srcs}
    if all(f.exists() for f in copies.values()) and len({f.read_bytes() for f in copies.values()}) > 1:
        print('::warning::.github/ballistic/ci.py differs between the two commits under test; '
              'a CI change has landed in one fork only')
    rows, ok = judge(results, list(LANES), a.expect, srcs, shas)
    ok = ok and all(lane in LANES for lane in results)
    loosened = []
    if a.base_sha:
        # A pull request may tighten the gate (an xfail flipped to pass, a new
        # baseline) but not relax it without the RULES_LABEL label.
        lib = workflow_lib()
        if lib is None or not SHA_RE.match(a.base_sha):
            die('--base-sha needs a full SHA and a blaspp or lapackpp repository')
        base = work / 'base' / lib
        fetch(getattr(a, f'{lib}_url'), a.base_sha, base, work / 'fetch.log')
        loosened, lnotes = rules_loosened(base, srcs[lib])
        for n in lnotes:
            print(f'::notice::{n}')
        for x in loosened:
            print(f'::{"warning" if a.rules_label == "true" else "error"}::gate relaxed: {x}')
        if loosened and a.rules_label != 'true':
            ok = False
    report(rows, ok, 'All lanes')
    if loosened:
        summary(f'### Gate relaxed by this pull request ({"accepted: label " + RULES_LABEL if a.rules_label == "true" else "not allowed without the label " + RULES_LABEL})\n\n'
                + '\n'.join(f'- {x}' for x in loosened))
    out = {'blaspp': shas['blaspp'], 'lapackpp': shas['lapackpp'], 'expect': a.expect, 'ok': ok,
           'subchecks': {sc: {'expected': want, 'outcome': got, 'verdict': v, 'reasons': rs, 'notes': ns}
                         for sc, want, got, v, rs, ns in rows}}
    Path(a.summary).write_text(json.dumps(out, indent=1), encoding='utf-8')
    sys.exit(0 if ok else 1)


def cmd_baseline(a):
    """Write .github/ballistic-baseline/<sub-check>.json for every gated
    sub-check of library a.lib in a run's downloaded `results-*` artifacts.
    The routines are re-parsed from the raw run_tests.py output in each
    artifact. Refuses a sub-check whose build, checks or consumer failed, and
    records failing routines as known failures only with --accept-failures."""
    dest = Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    written, refused = 0, 0
    for f in sorted(Path(a.results).rglob('result.json')):
        res = json.loads(f.read_text(encoding='utf-8'))
        for sc, rec in res['subchecks'].items():
            if SUB[sc]['lib'] != a.lib or SUB[sc]['tester'] != GATE:
                continue
            t = rec.get('tester')
            if not t or not t.get('present') or t.get('parse_error'):
                print(f'{sc}: no usable tester results; no baseline written')
                continue
            bad = [n for n, ph in rec['phases'].items() if ph['status'] != 'pass']
            if bad:
                print(f'{sc}: REFUSED, phases did not pass: {", ".join(bad)}')
                refused += 1
                continue
            out, xml = f.parent / 'tester' / f'{sc}.out.txt', f.parent / 'tester' / f'{sc}.xml'
            routines, details, err = parse_run_tests(
                out.read_text(encoding='utf-8', errors='replace'),
                xml.read_text(encoding='utf-8', errors='replace') if xml.exists() else None,
                t['exit'], './tester')
            if err or routines != t['routines']:
                print(f'{sc}: REFUSED, raw tester output does not reproduce result.json ({err})')
                refused += 1
                continue
            failed = [k for k, v in routines.items() if v != 'pass']
            if failed and not a.accept_failures:
                print(f'{sc}: REFUSED, failing routines {failed}; pass --accept-failures to record them')
                refused += 1
                continue
            data = {'subcheck': sc, 'run': a.run_url, 'blaspp': res['blaspp']['sha'],
                    'lapackpp': res['lapackpp']['sha'], 'args': t.get('args'),
                    'routines': routines, 'details': details}
            if sc in EXPORTS_CHECKED:
                if not rec.get('exports') or None in rec['exports'].values():
                    print(f'{sc}: REFUSED, no exported libraries/defines recorded')
                    refused += 1
                    continue
                data['exports'] = rec['exports']
            (dest / f'{sc}.json').write_text(json.dumps(data, indent=1) + '\n', encoding='utf-8')
            print(f'{sc}: {len(routines)} routines'
                  + (f', known failures: {[(k, details[k]["rows"]) for k in failed]}' if failed else ''))
            written += 1
    print(f'{written} baseline file(s) written to {dest}; {refused} refused')
    sys.exit(1 if refused else 0)


def cmd_selftest(a):
    import contextlib
    import io
    import tempfile
    failures = []

    def expect(cond, what):
        if not cond:
            failures.append(what)

    def quiet_judge(*args, **kw):
        # Keep the deliberate errors of these cases out of the run's annotations.
        with contextlib.redirect_stdout(io.StringIO()):
            return judge(*args, **kw)

    out = ('Mon\n./tester  --type s,d --dim 100 gemm\nhead\n   d   100  1e-16  pass\npass\n'
           './tester  --type c,z --dim 100 gemm\n   c   100  1e-16  pass\n   z   200  3.1  FAILED\n'
           '1 tests FAILED for gemm.\nFAILED: exit code 1\n'
           './tester  --dim 100 dot\npass\n')
    xml = ('<testsuites><testsuite><testcase name="gemm"><failure>1</failure></testcase>'
           '<testcase name="gemm">PASSED</testcase><testcase name="dot">PASSED</testcase>'
           '</testsuite></testsuites>')
    r, d, err = parse_run_tests(out, xml, 1, './tester')
    expect(err is None and r == {'gemm [s,d]': 'pass', 'gemm [c,z]': 'fail', 'dot': 'pass'}, f'parse: {r} {err}')
    expect(d['gemm [c,z]'] == {'n': 2, 'exit': 1, 'rows': ['#2 z 200']}, f'parse details: {d}')
    r, d, err = parse_run_tests(out, xml, 0, './tester')
    expect(err is not None, 'parse: wrong exit status accepted')
    r, d, err = parse_run_tests(out, xml.replace('<failure>1</failure>', ''), 1, './tester')
    expect(err is not None, 'parse: XML disagreement accepted')
    r, d, err = parse_run_tests(out + './tester  --dim 5 nrm2\n', xml, 1, './tester')
    expect(err is not None, 'parse: unterminated command accepted')
    r, d, err = parse_run_tests('', None, 0, './tester')
    expect(err is not None, 'parse: empty output accepted')
    out2 = './tester  --type s --dim 1 axpy\npass\n./tester  --type s --dim 2 axpy\npass\n'
    xml2 = ('<testsuites><testsuite><testcase name="axpy">PASSED</testcase>'
            '<testcase name="axpy">PASSED</testcase></testsuite></testsuites>')
    r, d, err = parse_run_tests(out2, xml2, 0, './tester')
    expect(err is None and list(r) == ['axpy [s]', 'axpy [s] #2'], f'parse repeats: {r}')

    def bl(routines, details=None):
        return {'routines': routines, 'details': details or {}}
    p, _ = compare_routines({'a': 'pass', 'b': 'fail'}, bl({'a': 'pass', 'b': 'pass'}))
    expect(p and 'b' in p[0], 'compare: regression missed')
    p, _ = compare_routines({'a': 'pass'}, bl({'a': 'pass', 'b': 'pass'}))
    expect(p, 'compare: missing routine missed')
    known = bl({'a': 'pass', 'b': 'fail'}, {'b': {'exit': 2, 'rows': ['#3 c 500', '#7 z 200']}})
    p, _ = compare_routines({'a': 'pass', 'b': 'fail'}, known, {'b': {'exit': 2, 'rows': ['#3 c 500', '#7 z 200']}})
    expect(not p, f'compare: known failure rejected: {p}')
    p, _ = compare_routines({'a': 'pass', 'b': 'fail'}, known, {'b': {'exit': 1, 'rows': ['#3 c 500']}})
    expect(not p, f'compare: narrower known failure rejected: {p}')
    p, _ = compare_routines({'a': 'pass', 'b': 'fail'}, known, {'b': {'exit': 1, 'rows': ['#1 s 100']}})
    expect(p, 'compare: new failed row of a known failure accepted')
    p, _ = compare_routines({'a': 'pass', 'b': 'fail'}, known, {'b': {'exit': -11, 'rows': ['#3 c 500']}})
    expect(p, 'compare: crash of a known failure accepted')
    p, _ = compare_routines({'a': 'pass', 'b': 'fail'}, bl({'a': 'pass', 'b': 'fail'}), {'b': {'exit': 1, 'rows': ['#1 x 1']}})
    expect(p, 'compare: known failure without recorded rows accepted')
    p, _ = compare_routines({'a': 'pass', 'c': 'fail'}, bl({'a': 'pass'}))
    expect(p, 'compare: new failure missed')
    p, _ = compare_routines({'a': 'fail'}, None)
    expect(p, 'compare: failure without baseline missed')
    p, _ = compare_routines({'a': 'pass'}, None, reference=bl({'a': 'pass', 'b': 'pass'}))
    expect(p, 'compare: routine missing from the reference set accepted')
    p, _ = compare_routines({'a': 'pass'}, None, {'a': {'n': 0}}, reference=bl({'a': 'pass'}, {'a': {'n': 12}}))
    expect(p, 'compare: routine without result rows accepted against the reference')
    p, _ = compare_routines({'a': 'pass'}, None, {'a': {'n': 3}}, reference=bl({'a': 'pass'}, {'a': {'n': 12}}))
    expect(not p, f'compare: fewer rows than a different-configuration reference rejected: {p}')
    p, _ = compare_routines({'a': 'pass'}, bl({'a': 'pass'}, {'a': {'n': 12}}), {'a': {'n': 0}})
    expect(p, 'compare: routine compiled out (0 rows) accepted against its baseline')
    p, _ = compare_routines({'a': 'pass'}, bl({'a': 'pass'}, {'a': {'n': 12}}), {'a': {'n': 11}})
    expect(p, 'compare: fewer result rows than the baseline accepted')
    p, _ = compare_routines({'a': 'pass'}, bl({'a': 'pass'}, {'a': {'n': 12}}), {'a': {'n': 12}})
    expect(not p, f'compare: same result rows rejected: {p}')
    blaspp_search = ('-- Looking for LAPACK libraries and options (lapack = )\n'
                     '   /usr/lib/libopenblas.so;-lm                yes\n'
                     '   Found LAPACK library: /usr/lib/libopenblas.so;-lm\n')
    expect(lapack_not_redetected(blaspp_search), 'rerun: a repeated LAPACK search was not noticed')
    expect(lapack_not_redetected('-- Looking for LAPACK libraries and options (lapack = )\n'
                                 '   Found LAPACK library: x\n') is None, 'rerun: a cached run was flagged')
    expect(lapack_not_redetected('-- Check for BLAS++\n   Found BLAS++: /x\n') is None,
           'rerun: output without a LAPACK search was flagged')
    _, d, _ = parse_run_tests(out, xml, 1, './tester')
    expect(d['gemm [s,d]']['n'] == 1 and d['dot']['n'] == 0 and d['gemm [c,z]']['n'] == 2,
           f'parse: row counts {d}')

    both = {'blaspp', 'lapackpp'}
    with tempfile.TemporaryDirectory() as tmp:
        srcs = {lib: Path(tmp) / lib for lib in ('blaspp', 'lapackpp')}
        for lib in srcs:
            (srcs[lib] / '.github').mkdir(parents=True)

        def write_exp(lib, overrides=None, drop=None, root=None):
            lines = []
            for sc in SUB:
                if repo_of(sc) != lib or sc == drop:
                    continue
                lines.append(f'{sc}: {(overrides or {}).get(sc, default_expectation(sc))}')
            path = Path(root or srcs[lib]) / EXPECTATIONS
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('\n'.join(lines) + '\n')

        def fake(lane, outcome_by_sc, tester_present=True, routines=None):
            res = {'lane': lane, 'infra': 'ok', 'blaspp': {'sha': 'b' * 40}, 'lapackpp': {'sha': 'l' * 40},
                   'subchecks': {}}
            for sc, _, mode in LANES[lane][1]:
                st = outcome_by_sc.get(sc, 'pass')
                rec = {'phases': {'build': {'status': st, 'note': ''}}, 'tester': None}
                if mode and st == 'pass':
                    rec['tester'] = ({'present': True, 'args': ['--x'], 'routines': routines or {'gemm [s]': 'pass'},
                                      'details': {}, 'parse_error': None} if tester_present else {'present': False})
                res['subchecks'][sc] = rec
            return res

        shas = {'blaspp': 'b' * 40, 'lapackpp': 'l' * 40}
        write_exp('blaspp')
        write_exp('lapackpp')
        base = {lane: fake(lane, {sc: 'fail' for sc, o, _ in LANES[lane][1] if o}) for lane in LANES}
        _, ok = quiet_judge(base, list(LANES), 'file', srcs, shas, both)
        expect(ok, 'judge: baseline-shaped results rejected')
        _, ok = quiet_judge(base, list(LANES), 'baseline', srcs, shas, both)
        expect(ok, 'judge: expect=baseline rejected baseline-shaped results')
        _, ok = quiet_judge(base, list(LANES), 'candidate', srcs, shas, both)
        expect(not ok, 'judge: expect=candidate accepted failures')
        missing = dict(base)
        del missing['2']
        _, ok = quiet_judge(missing, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: missing lane accepted')
        bad = dict(base)
        bad['1-cmake'] = fake('1-cmake', {}, routines={'gemm [s]': 'fail'})
        _, ok = quiet_judge(bad, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: failing routine accepted')
        bad['1-cmake'] = fake('1-cmake', {}, tester_present=False)
        _, ok = quiet_judge(bad, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: missing tester accepted')
        fixed = dict(base)
        fixed['5a'] = fake('5a', {})
        _, ok = quiet_judge(fixed, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: unexpected pass accepted')
        write_exp('blaspp', overrides={'5a.blaspp': 'pass', '5a.lapackpp': 'pass'})
        _, ok = quiet_judge(fixed, list(LANES), 'file', srcs, shas, both)
        expect(ok, 'judge: flipped entries rejected')
        _, ok = quiet_judge(base, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: flipped entry without the fix accepted')
        write_exp('blaspp', overrides={'1-cmake.blaspp': 'xfail:B1'})
        _, ok = quiet_judge(base, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: xfail on an unowned sub-check accepted')
        _, ok = quiet_judge(base, list(LANES), 'file', srcs, shas, {'lapackpp'})
        expect(ok, 'judge: a bad entry in the other fork\'s file is not lenient')
        exp, _, _ = load_expectations('file', srcs, {'lapackpp'})
        expect(exp['1-cmake.blaspp'] == 'pass', 'judge: lenient fallback kept an xfail on an unowned sub-check')
        write_exp('blaspp', drop='2.blaspp')
        _, ok = quiet_judge(base, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: missing entry accepted')
        write_exp('blaspp')
        (srcs['lapackpp'] / EXPECTATIONS).unlink()
        _, ok = quiet_judge(base, list(LANES), 'file', srcs, shas, both)
        expect(ok, 'judge: bootstrap (no lapackpp file) rejected')
        # A fork without F0 (no expectations file): its routine failures are
        # recorded, not gated, because it has no baseline to hold them to.
        pre = dict(base)
        pre['1-cmake'] = fake('1-cmake', {}, routines={'gemm [s]': 'fail'})
        pre['1-cmake']['subchecks']['1-cmake.lapackpp']['tester']['routines'] = {'getrf [s]': 'fail'}
        _, ok = quiet_judge(pre, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: failing routine of the fork with F0 accepted')
        pre['1-cmake'] = fake('1-cmake', {})
        pre['1-cmake']['subchecks']['1-cmake.lapackpp']['tester']['routines'] = {'getrf [s]': 'fail'}
        _, ok = quiet_judge(pre, list(LANES), 'file', srcs, shas, both)
        expect(ok, 'judge: failing routine of a fork without F0 not recorded')
        # Reference baseline: 5a.blaspp is held to 1-cmake.blaspp's.
        (srcs['blaspp'] / BASELINE_DIR).mkdir(parents=True, exist_ok=True)
        (srcs['blaspp'] / BASELINE_DIR / '1-cmake.blaspp.json').write_text(json.dumps(
            bl({'gemm [s]': 'pass', 'rotg [s]': 'fail'}, {'rotg [s]': {'exit': 1, 'rows': ['#2 z 200']}})))
        write_exp('blaspp', overrides={'5a.blaspp': 'pass', '5a.lapackpp': 'pass'})
        ref = dict(fixed)
        ref['5a'] = fake('5a', {})
        ref['1-cmake'] = fake('1-cmake', {})
        ref['1-cmake']['subchecks']['1-cmake.blaspp']['tester'].update(
            routines={'gemm [s]': 'pass', 'rotg [s]': 'fail'}, details={'rotg [s]': {'exit': 1, 'rows': ['#2 z 200']}})
        t5 = ref['5a']['subchecks']['5a.blaspp']['tester']
        t5.update(args=list(BLASPP_TESTS), routines={'gemm [s]': 'pass', 'rotg [s]': 'fail'},
                  details={'rotg [s]': {'exit': 1, 'rows': ['#2 z 200']}})
        _, ok = quiet_judge(ref, list(LANES), 'file', srcs, shas, both)
        expect(ok, 'judge: known failure of the reference baseline rejected')
        t5.update(routines={'gemm [s]': 'fail', 'rotg [s]': 'fail'},
                  details={'gemm [s]': {'exit': 1, 'rows': ['#1 s 1']}, 'rotg [s]': {'exit': 1, 'rows': ['#2 z 200']}})
        _, ok = quiet_judge(ref, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: regression against the reference baseline accepted')
        write_exp('blaspp')
        (srcs['blaspp'] / BASELINE_DIR / '1-cmake.blaspp.json').unlink()
        # Exported libraries/defines held to the baseline's form.
        exp4 = {'blaspp_libraries': '-la -lb;-lc;OpenMP::OpenMP_CXX', 'blaspp_defines': '-DX'}
        (srcs['blaspp'] / BASELINE_DIR / '4b.blaspp.json').write_text(json.dumps(
            dict(bl({'gemm [s]': 'pass'}, {'gemm [s]': {'n': 1}}), exports=exp4)))
        ex = dict(base)
        ex['4b'] = fake('4b', {})
        ex['4b']['subchecks']['4b.blaspp']['tester']['details'] = {'gemm [s]': {'n': 1}}
        ex['4b']['subchecks']['4b.blaspp']['exports'] = dict(exp4)
        _, ok = quiet_judge(ex, list(LANES), 'file', srcs, shas, both)
        expect(ok, 'judge: unchanged exports rejected')
        ex['4b']['subchecks']['4b.blaspp']['exports'] = dict(exp4, blaspp_libraries='-la;-lb;-lc;OpenMP::OpenMP_CXX')
        _, ok = quiet_judge(ex, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: changed exported libraries accepted')
        (srcs['blaspp'] / BASELINE_DIR / '4b.blaspp.json').unlink()
        stale = dict(base)
        stale['2'] = dict(base['2'], blaspp={'sha': 'c' * 40})
        _, ok = quiet_judge(stale, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: results for other commits accepted')
        infra = dict(base)
        infra['2'] = dict(base['2'], infra='error: x')
        _, ok = quiet_judge(infra, list(LANES), 'file', srcs, shas, both)
        expect(not ok, 'judge: infrastructure failure accepted')

        # Loosening between a pull request's base and head.
        b, h = Path(tmp) / 'base', Path(tmp) / 'head'
        for root in (b, h):
            write_exp('blaspp', root=root)
            (root / BASELINE_DIR).mkdir(parents=True)
            (root / BASELINE_DIR / 'x.json').write_text(json.dumps(
                bl({'a': 'pass', 'k': 'fail'}, {'k': {'exit': 1, 'rows': ['#1 c 5']}})))
        lo, _ = rules_loosened(b, h)
        expect(not lo, f'loosen: identical trees flagged: {lo}')
        write_exp('blaspp', overrides={'5a.blaspp': 'pass'}, root=h)
        lo, _ = rules_loosened(b, h)
        expect(not lo, f'loosen: an xfail flipped to pass flagged: {lo}')
        write_exp('blaspp', root=b, overrides={'5a.blaspp': 'pass'})
        write_exp('blaspp', root=h)
        lo, _ = rules_loosened(b, h)
        expect(lo, 'loosen: pass -> xfail missed')
        write_exp('blaspp', root=h, overrides={'5a.blaspp': 'pass'})
        (h / BASELINE_DIR / 'x.json').write_text(json.dumps(bl({'a': 'fail', 'k': 'fail'}, {
            'a': {'exit': 1, 'rows': ['#1 s 1']}, 'k': {'exit': 1, 'rows': ['#1 c 5']}})))
        lo, _ = rules_loosened(b, h)
        expect(lo, 'loosen: baseline pass -> fail missed')
        (h / BASELINE_DIR / 'x.json').write_text(json.dumps(
            bl({'a': 'pass', 'k': 'fail'}, {'k': {'exit': 2, 'rows': ['#1 c 5', '#2 z 9']}})))
        lo, _ = rules_loosened(b, h)
        expect(lo, 'loosen: wider known failure missed')
        (h / BASELINE_DIR / 'x.json').unlink()
        lo, _ = rules_loosened(b, h)
        expect(lo, 'loosen: deleted baseline missed')
        (h / BASELINE_DIR / 'x.json').write_text((b / BASELINE_DIR / 'x.json').read_text())
        def bla(r, d=None):
            return dict(bl(r, d), args=list(LAPACKPP_TESTS))

        ref = bla({'gesv [s]': 'pass', 'getrf [s]': 'pass'}, {'gesv [s]': {'n': 4}, 'getrf [s]': {'n': 6}})
        for root in (b, h):
            (root / BASELINE_DIR / '1-cmake.lapackpp.json').write_text(json.dumps(ref))
        lo, _ = rules_loosened(b, h)
        expect(not lo, f'loosen: unchanged reference flagged: {lo}')
        new = Path(h / BASELINE_DIR / '2.lapackpp.json')
        new.write_text(json.dumps(bla({'gesv [s]': 'fail', 'getrf [s]': 'pass'},
                                     {'gesv [s]': {'n': 4, 'exit': 1, 'rows': ['#1 s 1']}, 'getrf [s]': {'n': 6}})))
        lo, _ = rules_loosened(b, h)
        expect(lo, 'loosen: new baseline recording a failure missed (probe A)')
        new.write_text(json.dumps(dict({'routines': {}, 'details': {}}, args=list(LAPACKPP_TESTS))))
        lo, _ = rules_loosened(b, h)
        expect(lo, 'loosen: new empty baseline missed (probe B)')
        new.write_text(json.dumps(bla({'gesv [s]': 'pass', 'getrf [s]': 'pass'}, {'gesv [s]': {'n': 0}, 'getrf [s]': {'n': 6}})))
        lo, _ = rules_loosened(b, h)
        expect(lo, 'loosen: new baseline without result rows missed')
        new.write_text(json.dumps(bla({'gesv [s]': 'pass', 'getrf [s]': 'pass', 'potrf [s]': 'pass'},
                                     {'gesv [s]': {'n': 4}, 'getrf [s]': {'n': 6}, 'potrf [s]': {'n': 2}})))
        lo, _ = rules_loosened(b, h)
        expect(not lo, f'loosen: new passing baseline covering the reference flagged: {lo}')
        new.unlink()
        hx = json.loads((h / BASELINE_DIR / 'x.json').read_text())
        hx['routines']['added [d]'] = 'fail'
        (h / BASELINE_DIR / 'x.json').write_text(json.dumps(hx))
        lo, _ = rules_loosened(b, h)
        expect(lo, 'loosen: failing routine added to an existing baseline missed (probe C)')
        (h / BASELINE_DIR / 'x.json').write_text((b / BASELINE_DIR / 'x.json').read_text())
        (h / BASELINE_DIR / '1-cmake.lapackpp.json').write_text(json.dumps(
            bla(ref['routines'], {'gesv [s]': {'n': 0}, 'getrf [s]': {'n': 6}})))
        lo, _ = rules_loosened(b, h)
        expect(lo, 'loosen: baseline result rows dropping missed')
        (b / BASELINE_DIR / '1-cmake.lapackpp.json').write_text(json.dumps(dict(ref, exports={'v': 'a'})))
        (h / BASELINE_DIR / '1-cmake.lapackpp.json').write_text(json.dumps(dict(ref, exports={'v': 'b'})))
        lo, _ = rules_loosened(b, h)
        expect(lo, 'loosen: changed baseline exports missed')
        (b / BASELINE_DIR / '1-cmake.lapackpp.json').write_text(json.dumps(ref))
        (h / BASELINE_DIR / '1-cmake.lapackpp.json').write_text(json.dumps(ref))
        for root, text in ((b, 'a'), (h, 'b')):
            (root / CI_FILES[0]).parent.mkdir(parents=True, exist_ok=True)
            (root / CI_FILES[0]).write_text(text)
        lo, _ = rules_loosened(b, h)
        expect(lo, 'loosen: a ci.py change missed (M3)')

        # Review pass 3 probes, on fresh trees with F0.
        b2, h2 = Path(tmp) / 'b2', Path(tmp) / 'h2'
        bref = dict(bl({'a [s]': 'pass', 'b [s]': 'pass'}, {'a [s]': {'n': 3}, 'b [s]': {'n': 2}}),
                    args=list(BLASPP_TESTS))
        for root in (b2, h2):
            write_exp('blaspp', root=root)
            (root / BASELINE_DIR).mkdir(parents=True)
            (root / BASELINE_DIR / '1-cmake.blaspp.json').write_text(json.dumps(bref))
            (root / '.github' / 'ballistic').mkdir(parents=True)
            (root / '.github' / 'ballistic' / 'ci.py').write_text('same')
        lo, _ = rules_loosened(b2, h2)
        expect(not lo, f'probe: identical F0 trees flagged: {lo}')
        p1 = h2 / BASELINE_DIR / '5a.blaspp.json'
        p1.write_text(json.dumps({'subcheck': '5a.blaspp', 'args': ['--quick'], 'routines': {}, 'details': {}}))
        lo, _ = rules_loosened(b2, h2)
        expect(lo, 'probe P1: new empty baseline with non-default args accepted')
        rec = {'phases': {'build': {'status': 'pass', 'note': ''}},
               'tester': {'present': True, 'args': list(BLASPP_TESTS), 'routines': {'a [s]': 'pass'},
                          'details': {'a [s]': {'n': 0}}, 'parse_error': None}}
        got = outcome('5a.blaspp', rec, {'blaspp': h2, 'lapackpp': Path(tmp) / 'none'})
        expect(got[0] == 'fail', f'probe P1: a baseline recorded with other tester arguments accepted: {got}')
        p1.unlink()
        nan = dict(bref, details={'a [s]': {'n': float('nan')}, 'b [s]': {'n': 2}})
        (h2 / BASELINE_DIR / '1-cmake.blaspp.json').write_text(json.dumps(nan))
        lo, _ = rules_loosened(b2, h2)
        expect(lo, 'probe P2: NaN result-row count accepted')
        pr, _ = compare_routines({'a [s]': 'pass', 'b [s]': 'pass'}, json.loads(json.dumps(nan)),
                                 {'a [s]': {'n': 0}, 'b [s]': {'n': 2}})
        expect(pr, 'probe P2: a NaN baseline row count disabled the row floor')
        (h2 / BASELINE_DIR / '1-cmake.blaspp.json').write_text(json.dumps(dict(bref, args=['--quick', 'gemm'])))
        lo, _ = rules_loosened(b2, h2)
        expect(lo, 'probe P3: changed baseline tester arguments accepted')
        (h2 / BASELINE_DIR / '1-cmake.blaspp.json').write_text(json.dumps(bref))
        (h2 / '.github' / 'ballistic' / 'difflib.py').write_text('# planted')
        lo, _ = rules_loosened(b2, h2)
        expect(lo, 'probe P4: a new module next to ci.py accepted')
        (h2 / '.github' / 'ballistic' / 'difflib.py').unlink()
        (h2 / '.github' / 'workflows').mkdir(parents=True)
        (h2 / '.github' / 'workflows' / 'other.yml').write_text('jobs: {x: {name: check expectations}}')
        lo, _ = rules_loosened(b2, h2)
        expect(lo, 'probe P4: a new workflow accepted')
        (h2 / '.github' / 'workflows' / 'other.yml').unlink()
        (h2 / EXPECTATIONS).write_text((h2 / EXPECTATIONS).read_text() + '1-cmake.blaspp: xfail:B1\n')
        lo, _ = rules_loosened(b2, h2)
        expect(lo, 'probe P5: a duplicate expectation line that relaxes accepted')
        _, errs, _ = load_expectations('file', {'blaspp': h2, 'lapackpp': Path(tmp) / 'none'}, {'blaspp'})
        expect(errs, 'probe P5: a duplicate expectation line raised no error')
    if failures:
        for f in failures:
            print(f'SELFTEST FAILED: {f}')
        sys.exit(1)
    print('selftest: all checks passed')


def main():
    ap = argparse.ArgumentParser(description='BallisticLA fork CI')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('resolve')
    for name in ('lane', 'check'):
        p = sub.add_parser(name)
        if name == 'lane':
            p.add_argument('lane')
            p.add_argument('--out', required=True)
        else:
            p.add_argument('--results', required=True)
            p.add_argument('--summary', required=True)
            p.add_argument('--base-sha', default='')
            p.add_argument('--rules-label', default='false')
        p.add_argument('--work', required=True)
        p.add_argument('--expect', default='file', choices=['file', 'candidate', 'baseline'])
        for lib in ('blaspp', 'lapackpp'):
            p.add_argument(f'--{lib}-url', required=True)
            p.add_argument(f'--{lib}-sha', required=True)
    p = sub.add_parser('baseline')
    p.add_argument('--results', required=True)
    p.add_argument('--lib', required=True, choices=['blaspp', 'lapackpp'])
    p.add_argument('--dest', required=True)
    p.add_argument('--run-url', required=True)
    p.add_argument('--accept-failures', action='store_true',
                   help='record failing routines as known failures')
    sub.add_parser('selftest')
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        except Exception:
            pass
    {'resolve': cmd_resolve, 'lane': cmd_lane, 'check': cmd_check,
     'baseline': cmd_baseline, 'selftest': cmd_selftest}[a.cmd](a)


if __name__ == '__main__':
    main()
