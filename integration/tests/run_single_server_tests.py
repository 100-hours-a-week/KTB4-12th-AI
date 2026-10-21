"""Start one new loopback-only test PG, run scenarios, always stop our PG."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pg-bin',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dependency-path',action='append',default=[])
    parser.add_argument('--environment-root',type=Path)
    parser.add_argument('--encoder-assets',type=Path)
    parser.add_argument('--live', action='store_true', help='Explicit paid synthetic scenario; existing injected key and explicit synthetic test approval required')
    parser.add_argument('--live-case', choices=['baseline', 'exclusion'], default='baseline')
    args=parser.parse_args()
    if args.live_case != 'baseline' and not args.live:
        raise ValueError('Live case selection requires explicit --live')
    os.environ['SINGLE_SERVER_LIVE_CASE'] = args.live_case
    if args.live and (os.getenv('SINGLE_SERVER_LIVE_APPROVED') != '1'
                     or not os.getenv('OPENROUTER_API_KEY')
                     or not args.environment_root or not args.encoder_assets):
        raise ValueError('Explicit live selection and existing in-memory connection required')
    args.output.mkdir(parents=True,exist_ok=False)
    args.output.chmod(0o700)
    cluster=args.output/'cluster'
    sockets=tempfile.TemporaryDirectory(prefix='ss-pg-',dir='/tmp')
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    def command(argv):
        result=subprocess.run(argv,capture_output=True,text=True,timeout=45)
        with (args.output/'commands.jsonl').open('a') as stream:stream.write(json.dumps({'argv':argv,'exitCode':result.returncode})+'\n')
        if result.returncode:raise RuntimeError(result.stderr[-1500:])
    started=False
    try:
        command([str(args.pg_bin/'initdb'),'-D',str(cluster),'-U','fixture_owner','--auth-local=trust','--auth-host=trust','--no-locale','-E','UTF8'])
        command([str(args.pg_bin/'pg_ctl'),'-D',str(cluster),'-l',str(args.output/'postgres.log'),'-o',f'-h 127.0.0.1 -k {sockets.name} -p {port}','-w','start'])
        started=True
        if args.environment_root:
            for service in ('product-search', 'chat'):
                for site in (args.environment_root / service / '.venv/lib').glob('python*/site-packages'):
                    sys.path.append(str(site))
        for path in args.dependency_path:sys.path.append(path)
        sys.path[:0]=[str(ROOT/'chat'),str(ROOT/'profiling/src'),str(ROOT/'product-search'),str(ROOT)]
        import psycopg
        from psycopg import sql
        base=f'postgresql://fixture_owner@127.0.0.1:{port}/postgres'
        with psycopg.connect(base) as db:
            for name in ['ai_chat_service','ai_profiler_service','ai_search_service','ai_catalog_sync']:
                db.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Identifier(name)))
        os.environ['SINGLE_SERVER_TEST_CLUSTER_URL']=base
        os.environ['SINGLE_SERVER_TEST_PG_CTL']=str(args.pg_bin/'pg_ctl')
        os.environ['SINGLE_SERVER_TEST_PG_DATA']=str(cluster)
        os.environ['SINGLE_SERVER_TEST_OUTPUT']=str(args.output)
        if args.environment_root and args.encoder_assets:
            os.environ['SINGLE_SERVER_TEST_ENVIRONMENTS']=str(args.environment_root)
            os.environ['SINGLE_SERVER_TEST_ENCODER_ASSETS']=str(args.encoder_assets)
        os.environ['PYTHONDONTWRITEBYTECODE']='1'
        import pytest
        began=time.monotonic()
        target='live_single_server.py' if args.live else 'test_single_server.py'
        targets=[str(ROOT/'integration/tests'/target)]
        if not args.live:
            targets.extend(str(ROOT / 'integration/tests' / name) for name in ('test_admin_review_data.py', 'test_recipient_data.py'))
        code=pytest.main(['-q','-p','no:cacheprovider',*targets,
                          '--junitxml='+str(args.output/'junit.xml')])
        result={'exitCode':code,'seconds':time.monotonic()-began,'synthetic':True,
                'mode':'live' if args.live else 'free','existingApplicationDBChanged':False}
        if not args.live:result['paidCalls']=0
        (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        return code
    finally:
        if started:command([str(args.pg_bin/'pg_ctl'),'-D',str(cluster),'-m','fast','-w','stop'])
        sockets.cleanup()


if __name__=='__main__':raise SystemExit(main())
