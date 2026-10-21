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
}

EXPECTATIONS = '.github/ballistic-expectations.yml'
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
    # 4: oneMKL ILP64 named through BLAS_LIBRARIES, (a) as a semicolon list and
    # (b) as one space-separated string of -l flags. Both work upstream; B1
    # changes how they are split, so both must keep passing.
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
    ])),
    # 6b: LAPACK++ built with -D_GLIBCXX_ASSERTIONS; gemqrt with m = 0 and n = 0.
    ('6b', ('linux', [('6b.lapackpp.sanity', None, GATE), ('6b.lapackpp.zero-dims', 'L3', NONE)])),
    # 7: macOS new Accelerate; no LAPACKE, so build and consumer only.
    ('7a', ('macos', [('7a.blaspp', None, NONE), ('7a.lapackpp', None, NONE)])),
    ('7b', ('macos', [('7b.blaspp', None, NONE), ('7b.lapackpp', None, NONE)])),
    ('7c', ('macos', [('7c.lapackpp', None, NONE)])),
    # 8: MSVC + oneMKL ILP64 (NuGet) at a path with spaces. (a) BLAS_LIBRARIES
    # given to BLAS++ (B1); (b) LAPACK_LIBRARIES given to LAPACK++ (L1).
    # Testers are best effort until a recorded run sets their expectation.
    ('8a', ('windows', [('8a.blaspp', 'B1', RECORD), ('8a.lapackpp', 'B1', RECORD)])),
    ('8b', ('windows', [('8b.blaspp', None, RECORD), ('8b.lapackpp', 'L1', RECORD)])),
])

SUB = collections.OrderedDict()
for _lane, (_os, _subs) in LANES.items():
    for _sc, _owner, _tester in _subs:
        SUB[_sc] = {'lane': _lane, 'os': _os, 'lib': _sc.split('.')[1],
                    'owner': _owner, 'tester': _tester}

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
    r = subprocess.run(['git', 'ls-remote', url, 'refs/heads/main'], capture_output=True, text=True)
    sha = r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else ''
    if not SHA_RE.match(sha):
        die(f'cannot resolve main of {url}: {r.stderr.strip()}')
    return sha


# ------------------------------------------------------- tester output parser

def parse_run_tests(stdout_text, xml_text, exit_code, test_cmd):
    """Turn run_tests.py output into {routine key: 'pass' | 'fail'}.

    run_tests.py prints each tester command (starting with test_cmd), the
    tester's output, then a line 'pass' or 'FAILED: exit code N'. The key is
    the routine plus its --type value, with '#2', '#3' for repeats. The result
    is cross-checked against the --xml report and the exit status (the number
    of failed commands). Returns (routines, error); error is None when the
    three agree."""
    prefix = test_cmd + ' '
    entries, current = [], None
    for line in stdout_text.splitlines():
        if line.startswith(prefix):
            if current is not None:
                return {}, f'no result line for: {current}'
            current = line
        elif line == 'pass' or line.startswith('FAILED: exit code'):
            if current is None:
                return {}, f'result line without a command: {line}'
            entries.append((current, 'pass' if line == 'pass' else 'fail'))
            current = None
    if current is not None:
        return {}, f'run_tests.py stopped during: {current}'
    if not entries:
        return {}, 'run_tests.py ran no routines'
    routines, seen = collections.OrderedDict(), collections.Counter()
    for cmd, status in entries:
        toks = cmd.split()
        routine = toks[-1]
        types = toks[toks.index('--type') + 1] if '--type' in toks[:-1] else ''
        base = f'{routine} [{types}]' if types else routine
        seen[base] += 1
        routines[base if seen[base] == 1 else f'{base} #{seen[base]}'] = status
    if xml_text is None:
        return routines, 'run_tests.py wrote no XML report'
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return routines, f'unreadable XML report: {e}'
    from_xml = collections.Counter(
        (tc.get('name'), 'fail' if tc.find('failure') is not None else 'pass')
        for tc in root.iter('testcase'))
    from_out = collections.Counter((cmd.split()[-1], status) for cmd, status in entries)
    if from_xml != from_out:
        return routines, ('console output and XML report disagree: '
                          f'only in output {dict(from_out - from_xml)}, '
                          f'only in XML {dict(from_xml - from_out)}')
    nfail = sum(1 for _, s in entries if s == 'fail')
    if exit_code != nfail % 256:
        return routines, f'exit status {exit_code}, but {nfail} failed commands were parsed'
    return routines, None


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
        ctx = {'lib': lib, 'prefix': prefix, 'bdir': bdir, 'test_dir': bdir / 'test' if tester_built else None}
        self.run_checks(key, checks, ctx)
        if consumer:
            self.cmake_consumer(key, lib, name, [prefix] + list(deps), dll_dirs)
        if tester:
            self.run_tester(key, lib, bdir / 'test', tester_built,
                            tester_args or (BLASPP_TESTS if lib == 'blaspp' else LAPACKPP_TESTS),
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
                            BLASPP_TESTS if lib == 'blaspp' else LAPACKPP_TESTS, [])
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
            rec['tester'] = {'present': False}
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
        routines, err = parse_run_tests(text, xml_text, rc, test_cmd)
        if rc == 124:
            err = f'run_tests.py timed out after {timeout} s'
        failed = [k for k, v in routines.items() if v != 'pass']
        print(f'[{key}] tester: {len(routines)} routines, {len(failed)} failed'
              + (f': {", ".join(failed)}' if failed else '') + (f'; parse error: {err}' if err else ''))
        if failed or err:
            print(failing_blocks(text, test_cmd) if failed else tail(out_txt, 80))
        self.endgroup()
        rec['tester'] = {'present': True, 'exit': rc, 'routines': routines, 'parse_error': err}

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

    def reconfigure(self, key, lib, steps, final, deps=(), env=None, after=None):
        """Run the configure steps in one build tree (each step: (args, 'ok' |
        'fail') or a callable), then compare defines.h and <lib>Config.cmake
        with a fresh configure using `final`."""
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
                step()
                continue
            args, want = step
            rc = self.sh(['cmake', '-S', self.src[lib], '-B', tree, *base, *args], f'{key}.step{i}',
                         env=env, echo=True)
            self.snapshot(lib, tree, snaps / f'step{i}')
            if want == 'ok' and rc != 0:
                ok, note = False, f'configure step {i} failed'
            elif want == 'fail' and rc == 0:
                ok, note = False, f'precondition: configure step {i} was expected to fail but succeeded'
            i += 1
            if not ok:
                break
        self.endgroup()
        self.phase(key, 'reconfigure', ok, note, t0)
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


BLAS_MANGLING = defines_has_prefix('BLAS_FORTRAN_')
LAPACK_LINUX = defines_has('LAPACK_HAVE_LAPACK', 'LAPACK_VERSION', 'LAPACK_HAVE_LAPACKE')


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
        bp = r.cmake_lib(f'{lane}.blaspp', 'blaspp', f'{lane}-blaspp',
                         [f'-DBLAS_LIBRARIES={value}', '-Dblas_int=int64'],
                         checks=[BLAS_MANGLING, defines_has('BLAS_ILP64')])
        r.cmake_lib(f'{lane}.lapackpp', 'lapackpp', f'{lane}-lapackpp', [], deps=[bp],
                    checks=[LAPACK_LINUX, ldd_tester(has=[r'libmkl_gf_ilp64\.so'])])

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
        pre = r.cmake_lib('6b.lapackpp.sanity', 'lapackpp', '6b-lapackpp', ['-DCMAKE_CXX_FLAGS=-D_GLIBCXX_ASSERTIONS'],
                          deps=[bp], consumer=False, tester_args=['--quick', 'gemqrt'])
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
            r.cmake_lib('8b.lapackpp', 'lapackpp', '8b-lapackpp',
                        ['-Dgpu_backend=none', '-DLAPACK_LIBRARIES=' + ';'.join(spaced)], deps=[bp],
                        checks=[defines_has('LAPACK_HAVE_LAPACK'), config_has(dirname)])
    else:
        raise RuntimeError(f'no recipe for lane {lane}')


def zero_dims(r, key, built, test_dir):
    """gemqrt with m = 0 (side R) and n = 0 (side L): lwork is 0 there, and
    &work[0] on an empty std::vector trips _GLIBCXX_ASSERTIONS."""
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
        rc = r.sh(['./tester', *args, 'gemqrt'], f'{key}.run{i}', cwd=test_dir, echo=True, timeout=600)
        if rc != 0:
            bad.append(f'{" ".join(args)} -> exit {rc}')
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
    r.reconfigure('6.blaspp.identical', 'blaspp', [(blas, 'ok'), (blas, 'ok')], blas,
                  after=build_lapackpp_against)
    r.reconfigure('6.blaspp.fortran', 'blaspp', [(blas, 'ok'), (blas + ['-Dblas_fortran=gfortran'], 'ok')],
                  blas + ['-Dblas_fortran=gfortran'])
    conda = os.environ.get('CONDA_PREFIX')
    if conda:
        env = dict(r.env)
        env['LD_LIBRARY_PATH'] = os.pathsep.join([f'{conda}/lib'] + ([env['LD_LIBRARY_PATH']] if env.get('LD_LIBRARY_PATH') else []))
        sfx = [f'-DBLAS_LIBRARIES={conda}/lib/libopenblas64_.so', '-Dblas_int=int64']
        r.reconfigure('6.blaspp.suffix', 'blaspp', [(sfx, 'fail'), (sfx + ['-Dblas_symbol_suffix=64_'], 'ok')],
                      sfx + ['-Dblas_symbol_suffix=64_'], env=env)
    else:
        r.blocked('6.blaspp.suffix', ['reconfigure'], 'CONDA_PREFIX not set (conda OpenBLAS missing)')
    late = r.work / 'late-blas'
    shutil.rmtree(late, ignore_errors=True)
    late_lib = ['-DBLAS_LIBRARIES=' + str(late / 'libopenblas.so')]
    r.reconfigure('6.blaspp.failed-then-fixed', 'blaspp',
                  [(late_lib, 'fail'), lambda: copy_openblas(late), (late_lib, 'ok')], late_lib)

    # LAPACK++, against a fresh BLAS++: identical re-run; changed `lapack`
    # option; LAPACK_LIBRARIES that appears after a failed configure.
    bp = r.cmake_lib(None, 'blaspp', '6-blaspp', blas, consumer=False)
    r.reconfigure('6.lapackpp.identical', 'lapackpp', [([], 'ok'), ([], 'ok')], [], deps=[bp])
    r.reconfigure('6.lapackpp.option', 'lapackpp', [([], 'ok'), (['-Dlapack=default'], 'ok')],
                  ['-Dlapack=default'], deps=[bp])
    late2 = r.work / 'late-lapack'
    shutil.rmtree(late2, ignore_errors=True)
    late_lapack = ['-DLAPACK_LIBRARIES=' + str(late2 / 'libopenblas.so')]
    r.reconfigure('6.lapackpp.failed-then-fixed', 'lapackpp',
                  [(late_lapack, 'fail'), lambda: copy_openblas(late2), (late_lapack, 'ok')], late_lapack,
                  deps=[bp])


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


def load_expectations(mode, srcs):
    """Expected outcome per sub-check: 'pass' or 'xfail:<patch>'. srcs maps
    library -> source tree of the commit under test."""
    exp, errors, notes = {}, [], []
    if mode == 'candidate':
        return {sc: 'pass' for sc in SUB}, [], ['expect=candidate: every sub-check must pass']
    if mode == 'baseline':
        return ({sc: f'xfail:{SUB[sc]["owner"]}' if SUB[sc]['owner'] else 'pass' for sc in SUB}, [],
                ['expect=baseline: owned sub-checks must fail, the rest pass'])
    for repo in ('blaspp', 'lapackpp'):
        path = Path(srcs[repo]) / EXPECTATIONS
        where = f'BallisticLA/{repo}:{EXPECTATIONS}'
        if not path.exists():
            notes.append(f'{where} does not exist at the commit under test; treating that commit as '
                         'unmodified upstream (its owned sub-checks must fail, the rest pass)')
            for sc in SUB:
                if repo_of(sc) == repo:
                    exp[sc] = f'xfail:{SUB[sc]["owner"]}' if SUB[sc]['owner'] else 'pass'
            continue
        parsed, errs = parse_expectations(path.read_text(encoding='utf-8'), where)
        errors += errs + validate_expectations(parsed, repo, where)
        exp.update(parsed)
    return exp, errors, notes


def load_baseline(srcs, sc):
    path = Path(srcs[SUB[sc]['lib']]) / BASELINE_DIR / f'{sc}.json'
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))['routines']


def compare_routines(cur, base):
    """Problems (fail the sub-check) and notes, comparing per-routine results
    with the stored baseline. Without a baseline every routine must pass."""
    problems, notes = [], []
    if base is None:
        failed = [k for k, v in cur.items() if v != 'pass']
        if failed:
            problems.append(f'{len(failed)} routine(s) failed (no stored baseline): ' + ', '.join(failed[:8]))
        return problems, notes
    for k, b in base.items():
        c = cur.get(k)
        if c is None:
            problems.append(f'{k}: did not run (baseline {b})')
        elif b == 'pass' and c != 'pass':
            problems.append(f'{k}: regressed (baseline pass, now {c})')
        elif b != 'pass' and c == 'pass':
            notes.append(f'{k}: now passes (baseline {b})')
        elif b != 'pass':
            notes.append(f'{k}: known failure')
    for k, c in cur.items():
        if k not in base:
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
    mode, t = SUB[sc]['tester'], rec.get('tester')
    if mode == GATE:
        if not t or not t.get('present'):
            reasons.append('tester: expected but not built')
        elif t.get('parse_error'):
            reasons.append(f'tester: {t["parse_error"]}')
        else:
            problems, n = compare_routines(t['routines'], load_baseline(srcs, sc))
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


def judge(results, lanes, mode, srcs, shas):
    """Compare recorded results of `lanes` with the expectations. Returns
    (rows, ok)."""
    exp, errors, notes = load_expectations(mode, srcs)
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
            else:
                sha[lib], how[lib] = ls_remote_main(url[lib]), 'main (resolved now)'
        expect = os.environ.get('INPUT_EXPECT', '').strip() or 'file'
    else:
        sha[name], how[name] = os.environ['GITHUB_SHA'], f'{event} commit'
        sha[other(name)], how[other(name)] = ls_remote_main(url[other(name)]), 'main (resolved now)'
        expect = 'file'
    if expect not in ('file', 'candidate', 'baseline'):
        die(f'expect must be file, candidate or baseline, got {expect!r}')
    lines = [f'{lib}: {url[lib]} @ {sha[lib]}  [{how[lib]}]' for lib in ('blaspp', 'lapackpp')]
    lines.append(f'expectations: {expect}')
    print('\n'.join(lines))
    summary('### Commits under test\n\n' + '\n'.join(f'- {x}' for x in lines))
    for lib in ('blaspp', 'lapackpp'):
        gh_out(f'{lib}_url', url[lib])
        gh_out(f'{lib}_sha', sha[lib])
    gh_out('expect', expect)


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
    rows, ok = judge(results, list(LANES), a.expect, srcs, shas)
    ok = ok and all(lane in LANES for lane in results)
    report(rows, ok, 'All lanes')
    out = {'blaspp': shas['blaspp'], 'lapackpp': shas['lapackpp'], 'expect': a.expect, 'ok': ok,
           'subchecks': {sc: {'expected': want, 'outcome': got, 'verdict': v, 'reasons': rs, 'notes': ns}
                         for sc, want, got, v, rs, ns in rows}}
    Path(a.summary).write_text(json.dumps(out, indent=1), encoding='utf-8')
    sys.exit(0 if ok else 1)


def cmd_baseline(a):
    """Write .github/ballistic-baseline/<sub-check>.json for every gated
    sub-check of library a.lib whose tester ran and parsed cleanly."""
    results = load_results(a.results)
    dest = Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    for lane, res in results.items():
        for sc, rec in res['subchecks'].items():
            if SUB[sc]['lib'] != a.lib or SUB[sc]['tester'] != GATE:
                continue
            t = rec.get('tester')
            if not t or not t.get('present') or t.get('parse_error'):
                print(f'{sc}: no usable tester results; no baseline written')
                continue
            data = {'subcheck': sc, 'run': a.run_url, 'blaspp': res['blaspp']['sha'],
                    'lapackpp': res['lapackpp']['sha'], 'routines': t['routines']}
            (dest / f'{sc}.json').write_text(json.dumps(data, indent=1) + '\n', encoding='utf-8')
            failed = [k for k, v in t['routines'].items() if v != 'pass']
            print(f'{sc}: {len(t["routines"])} routines' + (f', known failures: {failed}' if failed else ''))
            written += 1
    print(f'{written} baseline file(s) written to {dest}')


def cmd_selftest(a):
    import tempfile
    failures = []

    def expect(cond, what):
        if not cond:
            failures.append(what)

    def judge(*args):
        # Keep the deliberate errors of these cases out of the run's annotations.
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return globals()['judge'](*args)

    out = ('Mon\n./tester  --type s,d --dim 100 gemm\nhead\n... pass\npass\n'
           './tester  --type c,z --dim 100 gemm\nFAILED: exit code 1\n'
           './tester  --dim 100 dot\npass\n')
    xml = ('<testsuites><testsuite><testcase name="gemm"><failure>1</failure></testcase>'
           '<testcase name="gemm">PASSED</testcase><testcase name="dot">PASSED</testcase>'
           '</testsuite></testsuites>')
    r, err = parse_run_tests(out, xml, 1, './tester')
    expect(err is None and r == {'gemm [s,d]': 'pass', 'gemm [c,z]': 'fail', 'dot': 'pass'}, f'parse: {r} {err}')
    r, err = parse_run_tests(out, xml, 0, './tester')
    expect(err is not None, 'parse: wrong exit status accepted')
    r, err = parse_run_tests(out, xml.replace('<failure>1</failure>', ''), 1, './tester')
    expect(err is not None, 'parse: XML disagreement accepted')
    r, err = parse_run_tests(out + './tester  --dim 5 nrm2\n', xml, 1, './tester')
    expect(err is not None, 'parse: unterminated command accepted')
    r, err = parse_run_tests('', None, 0, './tester')
    expect(err is not None, 'parse: empty output accepted')
    out2 = './tester  --type s --dim 1 axpy\npass\n./tester  --type s --dim 2 axpy\npass\n'
    xml2 = ('<testsuites><testsuite><testcase name="axpy">PASSED</testcase>'
            '<testcase name="axpy">PASSED</testcase></testsuite></testsuites>')
    r, err = parse_run_tests(out2, xml2, 0, './tester')
    expect(err is None and list(r) == ['axpy [s]', 'axpy [s] #2'], f'parse repeats: {r}')

    p, _ = compare_routines({'a': 'pass', 'b': 'fail'}, {'a': 'pass', 'b': 'pass'})
    expect(p and 'b' in p[0], 'compare: regression missed')
    p, _ = compare_routines({'a': 'pass'}, {'a': 'pass', 'b': 'pass'})
    expect(p, 'compare: missing routine missed')
    p, n = compare_routines({'a': 'pass', 'b': 'fail'}, {'a': 'pass', 'b': 'fail'})
    expect(not p, 'compare: known failure rejected')
    p, _ = compare_routines({'a': 'pass', 'c': 'fail'}, {'a': 'pass'})
    expect(p, 'compare: new failure missed')
    p, _ = compare_routines({'a': 'fail'}, None)
    expect(p, 'compare: failure without baseline missed')

    with tempfile.TemporaryDirectory() as tmp:
        srcs = {lib: Path(tmp) / lib for lib in ('blaspp', 'lapackpp')}
        for lib in srcs:
            (srcs[lib] / '.github').mkdir(parents=True)

        def write_exp(lib, overrides=None, drop=None):
            lines = []
            for sc in SUB:
                if repo_of(sc) != lib or sc == drop:
                    continue
                v = f'xfail:{SUB[sc]["owner"]}' if SUB[sc]['owner'] else 'pass'
                lines.append(f'{sc}: {(overrides or {}).get(sc, v)}')
            (srcs[lib] / EXPECTATIONS).write_text('\n'.join(lines) + '\n')

        def fake(lane, outcome_by_sc, tester_present=True, routines=None):
            res = {'lane': lane, 'infra': 'ok', 'blaspp': {'sha': 'b' * 40}, 'lapackpp': {'sha': 'l' * 40},
                   'subchecks': {}}
            for sc, _, mode in LANES[lane][1]:
                st = outcome_by_sc.get(sc, 'pass')
                rec = {'phases': {'build': {'status': st, 'note': ''}}, 'tester': None}
                if mode and st == 'pass':
                    rec['tester'] = ({'present': True, 'routines': routines or {'gemm [s]': 'pass'},
                                      'parse_error': None} if tester_present else {'present': False})
                res['subchecks'][sc] = rec
            return res

        shas = {'blaspp': 'b' * 40, 'lapackpp': 'l' * 40}
        write_exp('blaspp')
        write_exp('lapackpp')
        base = {lane: fake(lane, {sc: 'fail' for sc, o, _ in LANES[lane][1] if o}) for lane in LANES}
        _, ok = judge(base, list(LANES), 'file', srcs, shas)
        expect(ok, 'judge: baseline-shaped results rejected')
        _, ok = judge(base, list(LANES), 'baseline', srcs, shas)
        expect(ok, 'judge: expect=baseline rejected baseline-shaped results')
        _, ok = judge(base, list(LANES), 'candidate', srcs, shas)
        expect(not ok, 'judge: expect=candidate accepted failures')
        missing = dict(base)
        del missing['2']
        _, ok = judge(missing, list(LANES), 'file', srcs, shas)
        expect(not ok, 'judge: missing lane accepted')
        bad = dict(base)
        bad['1-cmake'] = fake('1-cmake', {}, routines={'gemm [s]': 'fail'})
        _, ok = judge(bad, list(LANES), 'file', srcs, shas)
        expect(not ok, 'judge: failing routine accepted')
        bad['1-cmake'] = fake('1-cmake', {}, tester_present=False)
        _, ok = judge(bad, list(LANES), 'file', srcs, shas)
        expect(not ok, 'judge: missing tester accepted')
        fixed = dict(base)
        fixed['5a'] = fake('5a', {})
        _, ok = judge(fixed, list(LANES), 'file', srcs, shas)
        expect(not ok, 'judge: unexpected pass accepted')
        write_exp('blaspp', overrides={'5a.blaspp': 'pass', '5a.lapackpp': 'pass'})
        _, ok = judge(fixed, list(LANES), 'file', srcs, shas)
        expect(ok, 'judge: flipped entries rejected')
        _, ok = judge(base, list(LANES), 'file', srcs, shas)
        expect(not ok, 'judge: flipped entry without the fix accepted')
        write_exp('blaspp', overrides={'1-cmake.blaspp': 'xfail:B1'})
        _, ok = judge(base, list(LANES), 'file', srcs, shas)
        expect(not ok, 'judge: xfail on an unowned sub-check accepted')
        write_exp('blaspp', drop='2.blaspp')
        _, ok = judge(base, list(LANES), 'file', srcs, shas)
        expect(not ok, 'judge: missing entry accepted')
        write_exp('blaspp')
        (srcs['lapackpp'] / EXPECTATIONS).unlink()
        _, ok = judge(base, list(LANES), 'file', srcs, shas)
        expect(ok, 'judge: bootstrap (no lapackpp file) rejected')
        stale = dict(base)
        stale['2'] = dict(base['2'], blaspp={'sha': 'c' * 40})
        _, ok = judge(stale, list(LANES), 'file', srcs, shas)
        expect(not ok, 'judge: results for other commits accepted')
        infra = dict(base)
        infra['2'] = dict(base['2'], infra='error: x')
        _, ok = judge(infra, list(LANES), 'file', srcs, shas)
        expect(not ok, 'judge: infrastructure failure accepted')
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
