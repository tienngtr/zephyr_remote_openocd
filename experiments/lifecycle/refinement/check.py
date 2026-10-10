# SPDX-License-Identifier: Apache-2.0
"""Run TLC, require exact expected results, and project its actual traces."""

# Preserve the previous experiment's verification workflow without editing it.
# pylint: disable=duplicate-code

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Case:
    name: str
    expected: str = 'safety pass'
    violation: str | None = None
    module: str = 'BoundaryProtocol'


CASES: tuple[Case, ...] = (
    Case('atomic-urgent', 'safety pass'),
    Case('backpressure-witness', 'reachable witness', 'NoBackpressureWitness'),
    Case('barrier-cycle', 'counterexample', 'NoBarrierCycle'),
    Case('barrier-deadlock', 'liveness counterexample', 'BarrierProgress'),
    Case('belief-cleanup', 'counterexample', 'ExclusiveCleanup'),
    Case('delayed-ack-witness', 'reachable witness', 'NoDelayedAckWitness'),
    Case('duplicate-ack-witness', 'reachable witness', 'NoDuplicateAckWitness'),
    Case('early-ack', 'counterexample', 'NoPrematureBarrierAck'),
    Case('early-retry', 'counterexample', 'RetrySettled'),
    Case('effect-cycle', 'counterexample', 'NoBarrierCycle'),
    Case('fair-closure', 'safety + liveness pass'),
    Case('fence-completion', 'safety + liveness pass', module='FenceLifetime'),
    Case('fence-unresolved', 'liveness counterexample', 'FenceEnds', 'FenceLifetime'),
    Case('fence-retarget', 'counterexample', 'NoStaleProposalCommit', 'FenceLifetime'),
    Case(
        'fence-supersession-witness',
        'reachable witness',
        'NoRetireStopWitness',
        'FenceLifetime',
    ),
    Case('generation-only', 'counterexample', 'NoInvalidExecution'),
    Case('late-acquire-witness', 'reachable witness', 'NoLateAcquireWitness'),
    Case('lost-ack-witness', 'reachable witness', 'NoLostAckWitness'),
    Case('lost-diagnostic', 'counterexample', 'AllErrorsRetained'),
    Case('output-cycle', 'counterexample', 'NoBarrierCycle'),
    Case('owner-gap', 'counterexample', 'ResourceOwned'),
    Case('retry-atomic', 'safety pass'),
    Case('retry-fair-closure', 'safety + liveness pass'),
    Case('retry-fence', 'safety pass'),
    Case('retry-no-fence', 'counterexample', 'NoObservedRetryOvertake'),
    Case('retry-wait-witness', 'reachable witness', 'NoRetryWaitWitness'),
    Case('revocation-witness', 'reachable witness', 'NoRevocationWitness'),
    Case('second-terminal', 'counterexample', 'OneTerminalOutput'),
    Case('shared-fence', 'safety pass'),
    Case('shared-no-fence', 'counterexample', 'NoObservedReadyOvertake'),
    Case('spawn-reject-witness', 'reachable witness', 'NoSpawnRejectWitness'),
    Case('split-spawn-output', 'counterexample', 'SpawnHasAdmission'),
    Case('stale-ack-witness', 'reachable witness', 'NoStaleAckWitness'),
    Case('stale-ack', 'counterexample', 'NoStaleTransfer'),
    Case('terminal-writer-witness', 'reachable witness', 'NoTerminalWriterWitness'),
    Case('timeout-settles', 'counterexample', 'NoFalseSettlement'),
    Case('unfair-admission', 'liveness counterexample', 'TerminationCloses'),
    Case('unfair-workers', 'liveness counterexample', 'TerminationCloses'),
    Case('unfair-writer', 'liveness counterexample', 'TerminationCloses'),
    Case('urgent-fence', 'safety pass'),
    Case('urgent-no-fence', 'counterexample', 'NoObservedReadyOvertake'),
    Case('writer-after-spawn-witness', 'reachable witness', 'NoWriterAfterSpawnWitness'),
    Case(
        'terminal-enqueued-writer-witness',
        'reachable witness',
        'NoTerminalEnqueuedWriterWitness',
        'BoundaryWitnesses',
    ),
    Case('late-offer-witness', 'reachable witness', 'NoLateOfferWitness', 'BoundaryWitnesses'),
    Case(
        'lost-ack-pending-witness',
        'reachable witness',
        'NoLostAckPendingWitness',
        'BoundaryWitnesses',
    ),
    Case('residual-witness', 'reachable witness', 'NoResidualWitness', 'BoundaryWitnesses'),
    Case(
        'writer-retry-block-witness',
        'reachable witness',
        'NoWriterRetryBlockWitness',
        'BoundaryWitnesses',
    ),
)

# Failures and witnesses finish before the larger exhaustive graphs.
CASES = tuple(sorted(CASES, key=lambda case: case.violation is None))


def split_fields(record: str) -> dict[str, str]:
    """Split only top-level TLC record fields, preserving nested values."""
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|<<|>>|[\[\]{}(),]|[^"<>{}\[\](),]+|.', record)
    depth = 0
    pieces: list[str] = []
    current: list[str] = []
    for token in tokens:
        if token in ('[', '{', '(', '<<'):
            depth += 1
        elif token in (']', '}', ')', '>>'):
            depth -= 1
        if token == ',' and depth == 0:
            pieces.append(''.join(current))
            current = []
        else:
            current.append(token)
    pieces.append(''.join(current))
    return {
        key.strip(): value.strip() for key, value in (piece.split('|->', 1) for piece in pieces)
    }


def trace_projection(case: Case, log: str) -> str:
    """Project actual TLC state records, including custody and buffer state."""
    states = list(re.finditer(r'^State (\d+): <([^\n]+)>\n', log, re.MULTILINE))
    if not states:
        raise RuntimeError(f'{case.name}: expected a TLC trace')
    lines = [
        f'# {case.name}',
        '',
        f'Expected {case.expected}: `{case.violation}`.',
        'Rows are actual TLC state projections; queue entries carry source/index tokens.',
        '',
        '| Step/action | Phase/gen; intent | Admission r/a/h; queues | Barrier/holds/receipts | '
        'Ticket stage; lease; quiet/released; cleanup | Ack/status | Protocol journal; buffer; '
        'writer/result slot | Primary; secondary diagnostics |',
        '| --- | --- | --- | --- | --- | --- | --- | --- |',
    ]
    if case.module == 'FenceLifetime':
        lines[-2:] = [
            '| Step/action | Phase/gen; terminal | Admission r/a/h; mailbox | '
            'Epoch/request; proposal/gen | Hold/ack/receipt | '
            'Ready/retry/settled | Journal | Audit |',
            '| --- | --- | --- | --- | --- | --- | --- | --- |',
        ]
    for index, state in enumerate(states):
        end = states[index + 1].start() if index + 1 < len(states) else len(log)
        block = log[state.end() : end]
        match = re.search(r'^st = (\[.*?\])\n\n', block, re.DOTALL | re.MULTILINE)
        if match is None:
            raise RuntimeError(f'{case.name}: missing TLC record')
        fields = split_fields(match[1][1:-1])

        # Keep full nested values; escape Markdown pipes without altering tokens.
        value = {
            name: re.sub(r'\s+', ' ', field).replace('|', '&#124;')
            for name, field in fields.items()
        }.__getitem__

        action = state[2].split(' line ')[0]
        if case.module == 'FenceLifetime':
            lines.append(
                f'| {state[1]} {action} | {value("phase")}/{value("generation")}; '
                f'{value("terminal")} | {value("recognized")}/{value("admitted")}/'
                f'{value("handled")}; {value("mailbox")} | {value("epoch")}/'
                f'{value("request")}; {value("kind")}/{value("requestGen")} | '
                f'{value("hold")}/{value("ackBox")}/{value("receipt")} | '
                f'{value("ready")}/{value("retry")}/{value("settled")} | '
                f'{value("journal")} | {value("audit")} |'
            )
            continue
        lines.append(
            f'| {state[1]} {action} | {value("phase")}/{value("generation")}; '
            f'{value("intent")} | {value("recognized")}/{value("admitted")}/'
            f'{value("handled")}; {value("mailboxes")} | {value("barrier")}; '
            f'{value("holds")}; {value("receipts")} | {value("stage")}; '
            f'{value("owner")}; {value("quiet")}/{value("released")}; '
            f'{value("cleanupActor")}; residual={value("residual")}; '
            f'result={value("cleanupResult")} | {value("transferAck")}; {value("statusQuery")} | '
            f'{value("journal")}; {value("output")}; {value("writerStage")}; '
            f'pending={value("writerPending")}; {value("writerFault")} | '
            f'{value("primary")}; {value("diagnostics")} |'
        )
    if 'Stuttering' in log or 'Back to state' in log:
        lines.extend(('', 'TLC closes this counterexample with a repeating/stuttering cycle.'))
    lines.append('')
    return '\n'.join(lines)


def run_case(
    case: Case, jar: Path, scratch: Path, artifacts: Path, reuse: bool, workers: int, heap: str
) -> str:
    root = Path(__file__).resolve().parent
    work = scratch / case.name
    temporary = work / 'java-tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    command = (
        'java',
        '-XX:+UseParallelGC',
        '-Xmx2g' if case.violation is not None else f'-Xmx{heap}',
        f'-Djava.io.tmpdir={temporary}',
        '-cp',
        str(jar),
        'tlc2.TLC',
        # Complete-graph temporal checking changes scheduling, not coverage.
        *(('-lncheck', 'final') if case.expected == 'safety + liveness pass' else ()),
        '-workers',
        '1' if case.violation is not None else str(workers),
        '-fp',
        '0',
        '-seed',
        '1',
        '-metadir',
        str(work / 'states'),
        '-config',
        str(root / f'{case.name}.cfg'),
        str(root / f'{case.module}.tla'),
    )
    inputs = {
        'spec': hashlib.sha256((root / 'BoundaryProtocol.tla').read_bytes()).hexdigest(),
        'config': hashlib.sha256((root / f'{case.name}.cfg').read_bytes()).hexdigest(),
        'jar': hashlib.sha256(jar.read_bytes()).hexdigest(),
    }
    if case.module != 'BoundaryProtocol':
        inputs['module'] = hashlib.sha256((root / f'{case.module}.tla').read_bytes()).hexdigest()
    manifest = work / 'result.json'
    cached = json.loads(manifest.read_text()) if reuse and manifest.exists() else {}
    expected_code = (
        0 if case.violation is None else (13 if case.expected == 'liveness counterexample' else 12)
    )
    if cached.get('inputs') == inputs and cached.get('returncode') == expected_code:
        print(f'Rechecking saved {case.name} ({case.expected})...', flush=True)
        returncode = cached['returncode']
        log = (work / 'tlc.log').read_text()
        if hashlib.sha256(log.encode()).hexdigest() != cached['log_sha256']:
            raise RuntimeError(f'{case.name}: saved TLC log has changed')
    else:
        print(f'Checking {case.name} ({case.expected})...', flush=True)
        with (work / 'tlc.log').open('w') as stream:
            result = subprocess.run(
                command, stdout=stream, stderr=subprocess.STDOUT, text=True, check=False
            )
        returncode = result.returncode
        log = (work / 'tlc.log').read_text()
        manifest.write_text(
            json.dumps(
                {
                    'inputs': inputs,
                    'returncode': returncode,
                    'log_sha256': hashlib.sha256(log.encode()).hexdigest(),
                },
                indent=2,
            )
            + '\n'
        )
    if case.violation is None:
        valid = returncode == 0 and 'Model checking completed. No error has been found.' in log
    elif case.expected == 'liveness counterexample':
        valid = returncode == 13 and 'Error: Temporal properties were violated.' in log
    else:
        valid = returncode == 12 and f'Error: Invariant {case.violation} is violated.' in log
    if not valid:
        raise RuntimeError(
            f'{case.name}: unexpected TLC result {returncode}; see {work / "tlc.log"}'
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
    parallelism = re.search(r'with (\d+) worker', log)
    memory = re.search(r'with (\d+)MB heap', log)
    optimistic = re.search(r'calculated \(optimistic\):\s+val = (\S+)', log)
    actual = re.search(r'based on the actual fingerprints:\s+val = (\S+)', log)
    if parallelism is None or memory is None:
        raise RuntimeError(f'{case.name}: TLC resource metadata missing')
    return (
        f'| {case.name} | {case.expected} | {case.violation or "all configured checks"} | '
        f'{generated} | {distinct} | {queued} | {depth[1] if depth else "—"} | '
        f'{parallelism[1]} | {memory[1]} | '
        f'{optimistic[1] if optimistic else "—"} | {actual[1] if actual else "—"} |'
    )


def main() -> None:
    """Verify exact expected outcomes, including intentionally broken variants."""
    repository = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jar', type=Path, required=True)
    parser.add_argument('--case', action='append', choices=[case.name for case in CASES])
    parser.add_argument('--reuse', action='store_true', help='Reverify matching saved TLC logs')
    parser.add_argument('--workers', type=int, default=1, choices=range(1, 17))
    parser.add_argument('--heap', choices=('2g', '6g'), default='2g')
    parser.add_argument(
        '--scratch',
        type=Path,
        default=repository / '.scratch/agents/architecture-refinement/checks',
    )
    args = parser.parse_args()
    jar = args.jar.resolve()
    scratch = args.scratch.resolve()
    artifacts = Path(__file__).resolve().parent / 'checked'
    artifacts.mkdir(exist_ok=True)
    cases = [case for case in CASES if args.case is None or case.name in args.case]
    rows = [
        run_case(case, jar, scratch, artifacts, args.reuse, args.workers, args.heap)
        for case in cases
    ]
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    summary = [
        '# Checked TLC results',
        '',
        'Generated by `check.py` from actual TLC output; failures below are deliberate.',
        f'Tools jar SHA-256: `{digest}`. Fixed fingerprint polynomial 0 and seed 1; '
        'resources are recorded per run.',
        'Specification SHA-256: `'
        + hashlib.sha256((artifacts.parent / 'BoundaryProtocol.tla').read_bytes()).hexdigest()
        + '`.',
        'Reachability module SHA-256: `'
        + hashlib.sha256((artifacts.parent / 'BoundaryWitnesses.tla').read_bytes()).hexdigest()
        + '`.',
        'Fence lifetime module SHA-256: `'
        + hashlib.sha256((artifacts.parent / 'FenceLifetime.tla').read_bytes()).hexdigest()
        + '`.',
        '',
        '| Configuration | Expected result (verified) | Property | Generated | Distinct | '
        'Queued at end | Depth | Workers | Heap MB | FP estimate (optimistic) | '
        'FP estimate (actual) |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |',
        *rows,
        '',
        'Passing runs exhaust their finite state graph. '
        'Counterexamples stop at the first violation.',
        'FP estimates are TLC\'s reported estimates of missed states due to '
        'fingerprint collisions, not an unbounded proof.',
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
