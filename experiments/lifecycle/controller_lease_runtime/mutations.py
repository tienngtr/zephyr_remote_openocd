# SPDX-License-Identifier: Apache-2.0
"""Run deliberately weakened rules against actual-runtime regressions."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASES = {
    'local-after-cancel': 'test_boundaries.py::test_local_launch_revalidates_cancellation',
    'early-retry': 'test_runtime.py::test_retry_requires_true_producer_settlement',
    'stale-adopt': 'test_runtime.py::test_stale_offer_cannot_replace_current_attempt',
    'lose-ownership': (
        'test_runtime.py::test_real_acquisition_interruption_keeps_cleanup_owner[acquired]'
    ),
    'replace-primary': (
        'test_runtime.py::test_primary_is_preserved_when_real_workspace_cleanup_fails'
    ),
    'attempt-after-terminal': 'test_runtime.py::test_terminal_authority_forbids_attempt_entry',
    'second-terminal': 'test_boundaries.py::test_terminal_writer_failure_no_second_result[False]',
    'timeout-success': 'test_boundaries.py::test_local_timeout_does_not_claim_remote_cleanup',
}


def main() -> None:
    scratch = Path('.scratch/agents/controller-lease-runtime/mutations')
    scratch.mkdir(parents=True, exist_ok=True)
    results: dict[str, object] = {}
    for mutation, node in CASES.items():
        command = [
            sys.executable,
            '-m',
            'pytest',
            str(ROOT / node),
            '-q',
            '-W',
            'error',
            '--timeout=60',
        ]
        normal = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        broken = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            env={**os.environ, 'ZRO_LEASE_MUTATION': mutation},
        )
        (scratch / (mutation + '.log')).write_text(broken.stdout + broken.stderr)
        semantic_failure = (
            broken.returncode == 1
            and 'AssertionError' in broken.stdout
            and 'Timeout (' not in broken.stdout
            and 'Timeout (' not in broken.stderr
        )
        assert normal.returncode == 0, normal.stdout + normal.stderr
        assert semantic_failure, broken.stdout + broken.stderr
        results[mutation] = {
            'baseline_passed': True,
            'semantic_assertion_failed': True,
            'node': node,
            'mutant_exit': broken.returncode,
        }
    results['sources_sha256'] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(ROOT.glob('*.py'))
    }
    (ROOT / 'MUTATIONS_CHECKED.json').write_text(json.dumps(results, indent=2) + '\n')
    print('Eight weakened rules rejected by semantic assertions; eight baselines passed.')


if __name__ == '__main__':
    main()
