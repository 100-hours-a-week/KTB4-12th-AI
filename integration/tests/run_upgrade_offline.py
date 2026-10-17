"""Run free regressions with existing service environments, without installs or providers."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment-root', type=Path, required=True,
                        help='Read-only source of existing chat/profiling/product-search .venv directories')
    parser.add_argument('--output', type=Path, required=True, help='New verification directory')
    parser.add_argument('--suites', nargs='+', choices=['chat', 'profiler', 'search', 'integration', 'javascript'])
    parser.add_argument('--pc-test-support', type=Path,
                        default=ROOT / 'chat/pc_tests')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = {k: v for k, v in os.environ.items()
           if not re.search(r'TOKEN|SECRET|PASSWORD|API_KEY|DATABASE_URL|^PROFILING_|^SEARCH_', k)}
    env.update(PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1')
    python = {s: str(args.environment_root.resolve() / s / '.venv/bin/python')
              for s in ('chat', 'profiling', 'product-search')}
    if not all(Path(p).is_file() for p in python.values()):
        raise ValueError('All three existing service environments are required; no automatic install')
    sites = sorted((args.environment_root.resolve() / 'product-search/.venv/lib').glob('python*/site-packages'))
    if len(sites) != 1:
        raise ValueError('Unambiguous existing Search Python site-packages required')
    common = ['-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'integration.tests.offline_guard']
    cases = [
        ('chat', ROOT / 'chat', [python['chat'], *common, 'tests', 'pc_tests'],
         [ROOT / 'chat', ROOT / 'chat/pc_tests', args.pc_test_support.resolve(), ROOT]),
        ('profiler', ROOT / 'profiling', [python['profiling'], *common, 'tests/unit'],
         [ROOT / 'profiling/src', ROOT]),
        ('search', ROOT / 'product-search', [python['product-search'], *common, 'tests'],
         [ROOT / 'product-search', ROOT]),
        ('integration', ROOT, [python['profiling'], *common,
         *[str(p.relative_to(ROOT)) for p in sorted((ROOT / 'integration/tests').glob('test_*.py'))], 'console/tests'],
         [ROOT, ROOT / 'profiling/src', ROOT / 'chat', ROOT / 'product-search', sites[0]]),
        ('javascript', ROOT, ['node', '--test', 'console/tests/core.test.js', 'console/tests/ops.test.js',
                             'product-search/tests/test_ui.mjs'], []),
    ]
    results = []
    for name, cwd, command, paths in cases:
        if args.suites and name not in args.suites:
            continue
        junit = output / (name + '.xml') if name != 'javascript' else None
        if junit is not None:
            command = [*command, f'--junitxml={junit}']
        test_env = {**env, 'PYTHONPATH': os.pathsep.join(map(str, paths)),
                    'INTEGRATION_SEARCH_TEST_PYTHON': python['product-search']}
        before = time.monotonic()
        with (output / (name + '.log')).open('w') as log:
            run = subprocess.run(command, cwd=cwd, env=test_env, stdout=log,
                                 stderr=subprocess.STDOUT, timeout=180)
        text = (output / (name + '.log')).read_text()
        result = {'suite': name, 'exitCode': run.returncode, 'seconds': time.monotonic() - before,
                  'cwd': str(cwd), 'command': command, 'PYTHONPATH': test_env['PYTHONPATH'],
                  'summary': '\n'.join(text.splitlines()[-5:])}
        if junit is not None and junit.is_file():
            suites = list(ET.parse(junit).getroot().iter('testsuite'))
            result['junit'] = str(junit)
            result['counts'] = {
                key: sum(int(suite.get(key, '0')) for suite in suites)
                for key in ('tests', 'failures', 'errors', 'skipped')
            }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    proof = {'passed': all(r['exitCode'] == 0 for r in results), 'results': results,
             'paidCalls': 0, 'realBEConnected': False, 'imagesBuilt': False}
    (output / 'result.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
    raise SystemExit(0 if proof['passed'] else 1)


if __name__ == '__main__':
    main()
