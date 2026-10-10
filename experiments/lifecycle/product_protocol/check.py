# SPDX-License-Identifier: Apache-2.0
"""Check fresh product-level specifications and retain actual TLC projections."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAFETY = (
    'LocalSafety ReadinessHonest TerminalSafety ResourceOwned RetrySettled NoStaleMutation '
    'PrimaryPreserved LocalPrimaryPreserved DiagnosticsRetained ResultHonest '
    'LocalResultHonest ChildFailureRecorded OneShotSafety TerminalUnique '
    'ProtocolClosed ClosureAccountsResources OutputBoundedOrdered'
)


@dataclass(frozen=True)
class Case:
    name: str
    candidate: str = 'lease'
    broken: str = 'none'
    property: str | None = None
    temporal: bool = False
    spec: str = 'Spec'
    module: str = 'ProductSession'
    mode: str = 'server'
    exitcode: int = 7


CASES = (
    Case('v1', 'v1'),
    Case('authority', 'authority'),
    Case('lease'),
    Case('oneshot-success', mode='oneshot', exitcode=0),
    Case('oneshot-failure', mode='oneshot'),
    Case('lease-fair', temporal=True, spec='FairSpec'),
    Case('unfair', property='TerminationCloses', temporal=True),
    Case('workers-unfair', property='TerminationCloses', temporal=True, spec='WorkersUnfairSpec'),
    Case('late-ready-cleaned', property='NoLateReadyWitness'),
    Case('authority-overtake', 'authority', property='NoReadyOvertakeWitness'),
    Case('lease-overtake', property='NoReadyOvertakeWitness'),
    Case('lease-retry-overtake', property='NoRetryOvertakeWitness'),
    Case('requested-winner', property='NoRequestedWinner'),
    Case('natural-winner', property='NoNaturalWinner'),
    Case('late-acquisition', property='NoLateAcquireWitness'),
    Case('late-client', broken='late-client', property='LocalSafety'),
    Case('early-retry', broken='early-retry', property='RetrySettled'),
    Case('lost-owner', broken='lost-owner', property='ResourceOwned'),
    Case('stale-adopt', broken='stale-adopt', property='NoStaleMutation'),
    Case('status-mix', broken='status-mix', property='ResultHonest'),
    Case('replace-primary', broken='replace-primary', property='PrimaryPreserved'),
    Case('lost-diagnostic', broken='lost-diagnostic', property='DiagnosticsRetained'),
    Case('second-terminal', broken='second-terminal', property='TerminalUnique'),
    Case('terminal-attempt', broken='terminal-attempt', property='TerminalSafety'),
    Case('stream-order', module='OutputOrder'),
    *(
        Case(
            name,
            property='NoCompleteWitness',
            spec='HistorySpec',
            module='ProductHistories',
            exitcode=0,
        )
        for name in ('benign-ready', 'benign-requested', 'benign-natural', 'benign-late-acquire')
    ),
)


def configuration(case: Case) -> str:
    if case.module == 'OutputOrder':
        return 'SPECIFICATION Spec\nCHECK_DEADLOCK FALSE\nINVARIANT BoundedOrdered\n'
    text = (
        f'SPECIFICATION {case.spec}\nCONSTANTS Candidate = "{case.candidate}"\n'
        f' Broken = "{case.broken}"\n MaxChunks = 0\n Mode = "{case.mode}"\n'
        f' ExitCode = {case.exitcode}\nCHECK_DEADLOCK FALSE\n'
    )
    if case.module == 'ProductHistories':
        return (
            text
            + f'CONSTANT History = "{case.name}"\n'
            + f'INVARIANTS {SAFETY} HistorySound NoCompleteWitness\n'
        )
    if case.property is not None:
        return text + f'{"PROPERTY" if case.temporal else "INVARIANT"} {case.property}\n'
    return (
        text
        + f'INVARIANTS {SAFETY}'
        + (' ObservedPrecedence' if case.candidate == 'v1' else '')
        + '\n'
        + ('PROPERTY TerminationCloses\n' if case.temporal else '')
    )


def fields(record: str) -> dict[str, str]:
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|<<|>>|[\[\]{}(),]|[^"<>{}\[\](),]+|.', record)
    depth = 0
    pieces: list[str] = []
    part: list[str] = []
    for token in tokens:
        if token in ('[', '{', '(', '<<'):
            depth += 1
        elif token in (']', '}', ')', '>>'):
            depth -= 1
        if token == ',' and depth == 0:
            pieces.append(''.join(part))
            part = []
        else:
            part.append(token)
    pieces.append(''.join(part))
    return {
        key.strip(): re.sub(r'\s+', ' ', value).replace('|', '&#124;')
        for key, value in (piece.split('|->', 1) for piece in pieces)
    }


def projection(log: str, case: Case) -> str:
    rows = [
        f'# {case.name}',
        '',
        f'Actual TLC violation: `{case.property}`.',
        '',
        '| Step/action | Local/remote/gen | Attempt custody | Control | Protocol | Outcome |',
        '| --- | --- | --- | --- | --- | --- |',
    ]
    states = list(re.finditer(r'^State (\d+): <([^\n]+)>\n', log, re.MULTILINE))
    assert states, 'expected actual counterexample states'
    for index, state in enumerate(states):
        end = states[index + 1].start() if index + 1 < len(states) else len(log)
        match = re.search(
            r'^(?:/\\ )?s = (\[.*?\])\n(?=/\\ |\n)',
            log[state.end() : end],
            re.DOTALL | re.MULTILINE,
        )
        assert match is not None
        value = fields(match[1][1:-1])
        action = state[2].split(' line ')[0]
        if case.module == 'ProductHistories':
            last = re.search(r'^/\\ last = "([^"\n]+)"', log[state.end() : end], re.MULTILINE)
            assert last is not None
            action = last[1]  # actual TLC state instrumentation, not a reconstructed story
        rows.append(
            f'| {state[1]} {action} | '
            f'{value["local"]}/{value["remote"]}/{value["gen"]} | {value["attempts"]} | '
            f'{value["loss"]} | committed={value["protocolLog"]}; '
            f'buffer={value["wire"]}; final={value["terminal"]}; '
            f'final-received={value["endedReceived"]}; '
            f'client={value["client"]} | {value["cause"]}; '
            f'child={value["childResult"]}; primary={value["primary"]}; '
            f'diagnostics={value["diagnostics"]}; local-primary={value["localPrimary"]}; '
            f'local-diagnostics={value["localDiagnostics"]} |'
        )
    if 'Stuttering' in log or 'Back to state' in log:
        rows.extend(('', 'TLC ends this trace in a repeating/stuttering cycle.'))
    return '\n'.join((*rows, ''))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jar', type=Path, required=True)
    parser.add_argument('--case', action='append', choices=[case.name for case in CASES])
    args = parser.parse_args()
    jar = args.jar.resolve()
    scratch = ROOT.parents[2] / '.scratch/agents/product-semantics/tlc'
    checked = ROOT / 'checked'
    checked.mkdir(exist_ok=True)
    cases = [case for case in CASES if args.case is None or case.name in args.case]

    def run_case(case: Case) -> str:
        config = ROOT / (case.name + '.cfg')
        config.write_text(configuration(case))
        work = scratch / case.name
        work.mkdir(parents=True, exist_ok=True)
        java_tmp = work / 'java-tmp'
        java_tmp.mkdir(exist_ok=True)
        print(f'Checking {case.name}...', flush=True)
        with (work / 'tlc.log').open('w') as stream:
            result = subprocess.run(
                [
                    'java',
                    f'-Djava.io.tmpdir={java_tmp}',
                    '-XX:+UseParallelGC',
                    '-Xmx1g',
                    '-cp',
                    str(jar),
                    'tlc2.TLC',
                    '-workers',
                    '1',
                    '-fp',
                    '0',
                    '-seed',
                    '1',
                    '-lncheck',
                    'final',
                    '-metadir',
                    str(work / 'states'),
                    '-config',
                    str(config),
                    str(ROOT / (case.module + '.tla')),
                ],
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        log = (work / 'tlc.log').read_text()
        if case.property is None:
            assert result.returncode == 0 and 'No error has been found' in log, case.name
            expected = 'Safety + fair closure' if case.temporal else 'Safety pass'
        else:
            code = 13 if case.temporal else 12
            message = (
                'Temporal properties were violated'
                if case.temporal
                else (f'Invariant {case.property} is violated')
            )
            assert result.returncode == code and message in log, case.name
            (checked / (case.name + '.md')).write_text(projection(log, case))
            expected = (
                'Expected counterexample'
                if case.broken != 'none' or case.temporal
                else ('Permitted reachable history')
            )
        count = re.findall(
            r'([\d,]+) states generated, ([\d,]+) distinct states found, '
            r'([\d,]+) states left on queue',
            log,
        )[-1]
        digest = hashlib.sha256(config.read_bytes()).hexdigest()
        return f'| {case.name} | {expected} | {count[0]} | {count[1]} | {count[2]} | `{digest}` |'

    with ThreadPoolExecutor(max_workers=3) as executor:
        rows = list(executor.map(run_case, cases))
    hashes = '\n'.join(
        f'- {name}: `{hashlib.sha256((ROOT / name).read_bytes()).hexdigest()}`'
        for name in ('ProductSession.tla', 'OutputOrder.tla', 'ProductHistories.tla')
    )
    (checked / ('SUMMARY.md' if args.case is None else 'SELECTED.md')).write_text(
        '# Checked TLC results\n\nGenerated from actual TLC runs. Up to three independent '
        'checks concurrently, each with one worker and 1 GiB heap, '
        'fixed fingerprint 0 and seed 1.\n\n'
        + hashes
        + '\n'
        + f'- tools jar: `{hashlib.sha256(jar.read_bytes()).hexdigest()}`\n\n'
        + '| Configuration | Checked result | Generated | Distinct | Queued | Config SHA-256 |\n'
        + '| --- | --- | --- | --- | --- | --- |\n'
        + '\n'.join(rows)
        + '\n\nPassing runs exhaust these finite graphs. Expected violations produce '
        'actual trace projections. This is bounded checking, not an unbounded proof.\n'
    )
    print(f'Verified {len(cases)} TLC results.', flush=True)


if __name__ == '__main__':
    main()
