"""Read-only readiness report. Never replays 0007 or changes a role/grant."""
import json
import os
import psycopg


def audit(url):
    with psycopg.connect(url.replace('postgresql+psycopg://','postgresql://'),
            connect_timeout=5, options='-c default_transaction_read_only=on') as db:
        version=db.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        superuser=db.execute('SELECT rolsuper FROM pg_roles WHERE rolname=current_user').fetchone()[0]
        checks={}
        for table,privilege in [('ai_profile.recipient_profiles','SELECT'),
                ('ai_profile.profile_runs','SELECT'),('ai_profile.profile_runs','INSERT'),
                ('ai_profile.profile_runs','UPDATE'),('ai_profile.recipient_reviews','DELETE')]:
            checks[table+':'+privilege]=db.execute('SELECT has_table_privilege(current_user,%s,%s)',(table,privilege)).fetchone()[0]
        return {'migration':version,'superuser':superuser,'checks':checks,
                'ready':version=='0011' and not superuser and all(checks.values()),
                'changed':False,'note':'0011 is required. Changes to 0007 do not replay on an existing DB; no role/grant repair is performed.'}


if __name__=='__main__':
    print(json.dumps(audit(os.environ['PROFILING_DATABASE_URL']),indent=2))
