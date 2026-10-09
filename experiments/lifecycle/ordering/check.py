# SPDX-License-Identifier: Apache-2.0
"""Run TLC, require exact expected results, and project its actual traces."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Case:
    name: str
    expected: str = 'safety pass'
    violation: str | None = None


CASES = (
    Case('dispatch-ready', 'counterexample', 'NoObservedReadyOvertake'),
    Case('dispatch-retry', 'counterexample', 'NoObservedRetryOvertake'),
    Case('ack-only', 'counterexample', 'NoObservedReadyOvertake'),
    Case('early-ack', 'counterexample', 'NoObservedReadyOvertake'),
    Case('no-drain', 'counterexample', 'NoObservedReadyOvertake'),
    Case('irrevocable', 'counterexample', 'NoInvalidExecution'),
    Case('generation-only', 'counterexample', 'NoInvalidExecution'),
    Case('early-retry', 'counterexample', 'RetryAfterSettlement'),
    Case('batch-witness', 'reachable witness', 'NoBatchWitness'),
    Case('pending-effect-witness', 'reachable witness', 'NoPendingEffectWitness'),
    Case('late-completion-witness', 'reachable witness', 'NoLateCompletionWitness'),
    Case('stale-rejection-witness', 'reachable witness', 'NoStaleRejectionWitness'),
    Case('diagnostics-witness', 'reachable witness', 'NoDiagnosticsWitness'),
    Case('unfair-liveness', 'liveness counterexample', 'TerminationCloses'),
    Case('workers-unfair-liveness', 'liveness counterexample', 'TerminationCloses'),
    Case('atomic-liveness', 'safety + liveness pass'),
    Case('fence-liveness', 'safety + liveness pass'),
    Case('atomic'),
    Case('fence'),
)


def trace_projection(case: Case, log: str) -> str:
    """Project serialized TLC states; never invent or replay a model history."""
    states = list(re.finditer(r'^State (\d+): <([^\n]+)>\n', log, re.MULTILINE))
    if not states:
        raise RuntimeError(f'{case.name}: expected a TLC trace')
    lines = [
        f'# {case.name}',
        '',
        f'Expected {case.expected}: `{case.violation}`.',
        'Each row is a projection of an actual TLC state; r/a/h means recognized/admitted/handled.',
        '',
        '| Step/action | Phase/gen | Intent/primary | Control; interrupt r/a/h | Barrier | '
        'Tickets (owner) | Protocol | Secondary diagnostics | Late rejection |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- |',
    ]
    for index, state in enumerate(states):
        end = states[index + 1].start() if index + 1 < len(states) else len(log)
        block = log[state.end() : end].split(' states generated,')[0]
        fields = dict(re.findall(r'/\\ (\w+) = (.*?)(?=\n/\\|\Z)', block, re.DOTALL))
        phase = fields['phase'].strip().strip('"')
        generation = fields['generation'].strip()
        intent = fields['intent'].strip().strip('"')
        primary = re.search(r'code \|-> "([^"]+)"', fields['primary'])
        if primary is None:
            raise RuntimeError('TLC primary is not a diagnostic record')
        positions = []
        for source in ('control', 'interrupt'):
            counts = []
            for field in ('recognized', 'admitted', 'handled'):
                match = re.search(source + r' \|-> (\d+)', fields[field])
                if match is None:
                    raise RuntimeError(f'TLC trace lacks {field}/{source}')
                counts.append(match[1])
            positions.append('/'.join(counts))
        stages = re.findall(r'<<(\d+), "([^"]+)">> :> "([^"]+)"', fields['stage'])
        owners = {
            (g, name): value
            for g, name, value in re.findall(r'<<(\d+), "([^"]+)">> :> "([^"]+)"', fields['owner'])
        }
        tickets = (
            ', '.join(
                f'{g}/{name}:{value}({owners[g, name]})'
                for g, name, value in sorted(stages)
                if value != 'absent'
            )
            or '—'
        )
        entries = re.findall(
            r'kind \|-> "([^"]+)".*?generation \|-> (\d+)', fields['protocol'], re.DOTALL
        )
        protocol = ', '.join(f'{kind}({g})' for kind, g in entries)
        secondary = ', '.join(re.findall(r'code \|-> "([^"]+)"', fields['diagnostics'])) or '—'
        if 'stream-close-detail' in fields['diagnostics']:
            secondary += ' [stream-close-detail retained]'
        action = state[2].split(' line ')[0]
        late = (
            ', '.join(
                re.findall(
                    r'(lateDispatchRejected|lateCompletionRejected) \|-> TRUE', fields['audit']
                )
            )
            or '—'
        )
        lines.append(
            f'| {state[1]} {action} | {phase}/{generation} | {intent}/{primary[1]} | '
            f'{"; ".join(positions)} | {fields["barrier"].strip().strip(chr(34))} | '
            f'{tickets} | {protocol} | {secondary} | {late} |'
        )
    if 'Stuttering' in log or 'Back to state' in log:
        lines.extend(('', 'TLC closes this counterexample with a repeating/stuttering cycle.'))
    lines.append('')
    return '\n'.join(lines)


def run_case(case: Case, jar: Path, scratch: Path, artifacts: Path) -> str:
    root = Path(__file__).resolve().parent
    work = scratch / case.name
    temporary = work / 'java-tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    command = (
        'java',
        '-XX:+UseParallelGC',
        '-Xmx2g',
        f'-Djava.io.tmpdir={temporary}',
        '-cp',
        str(jar),
        'tlc2.TLC',
        '-workers',
        '1',
        '-fp',
        '0',
        '-seed',
        '1',
        '-metadir',
        str(work / 'states'),
        '-config',
        str(root / f'{case.name}.cfg'),
        str(root / 'LifecycleOrdering.tla'),
    )
    print(f'Checking {case.name} ({case.expected})...', flush=True)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    log = result.stdout + result.stderr
    (work / 'tlc.log').write_text(log)
    if case.violation is None:
        valid = (
            result.returncode == 0 and 'Model checking completed. No error has been found.' in log
        )
    elif case.expected == 'liveness counterexample':
        valid = result.returncode == 13 and 'Error: Temporal properties were violated.' in log
    else:
        valid = result.returncode == 12 and f'Error: Invariant {case.violation} is violated.' in log
    if not valid:
        raise RuntimeError(
            f'{case.name}: unexpected TLC result {result.returncode}; see {work / "tlc.log"}'
        )
    if case.violation is not None:
        (artifacts / f'{case.name}.md').write_text(trace_projection(case, log))
    statistics = re.findall(
        r'([\d,]+) states generated, ([\d,]+) distinct states found, '
        r'([\d,]+) states left on queue.',
        log,
    )
    if not statistics:
        raise RuntimeError(f'{case.name}: TLC statistics missing')
    generated, distinct, queued = statistics[-1]
    depth = re.search(r'depth of the complete state graph search is (\d+)', log)
    return (
        f'| {case.name} | {case.expected} | {case.violation or "all configured checks"} | '
        f'{generated} | {distinct} | {queued} | {depth[1] if depth else "—"} |'
    )


def main() -> None:
    """Verify exact expected outcomes, including intentionally broken variants."""
    repository = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jar', type=Path, required=True)
    parser.add_argument('--case', action='append', choices=[case.name for case in CASES])
    parser.add_argument(
        '--scratch', type=Path, default=repository / '.scratch/agents/architecture-ordering/checks'
    )
    args = parser.parse_args()
    jar = args.jar.resolve()
    scratch = args.scratch.resolve()
    artifacts = Path(__file__).resolve().parent / 'checked'
    artifacts.mkdir(exist_ok=True)
    cases = [case for case in CASES if args.case is None or case.name in args.case]
    rows = [run_case(case, jar, scratch, artifacts) for case in cases]
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    summary = [
        '# Checked TLC results',
        '',
        'Generated by `check.py` from actual TLC output; failures below are deliberate.',
        f'Tools jar SHA-256: `{digest}`. One worker, fixed fingerprint polynomial 0 and seed 1.',
        '',
        '| Configuration | Expected result (verified) | Property | Generated | Distinct | '
        'Queued at end | Depth |',
        '| --- | --- | --- | --- | --- | --- | --- |',
        *rows,
        '',
        'Passing runs exhaust their finite state graph. '
        'Counterexamples stop at the first violation.',
        'Witness configurations negate a reachability target; '
        'their expected violations are coverage evidence.',
        'See the [report](../REPORT.md) for bounds, fairness assumptions, and limitations.',
        '',
    ]
    name = 'SUMMARY.md' if args.case is None else 'SELECTED.md'
    (artifacts / name).write_text('\n'.join(summary))
    print(f'Verified {len(cases)} expected TLC outcomes.', flush=True)


if __name__ == '__main__':
    main()
