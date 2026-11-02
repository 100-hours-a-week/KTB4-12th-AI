"""Migration targets follow the same explicit-file precedence as app secrets."""
import pytest

from integration.database_identity import validate_database_name
from integration.migrate_single_server import migration_url


def test_migration_file_wins_over_inherited_url(tmp_path, monkeypatch):
    path = tmp_path / 'dsn'
    path.write_text('postgresql://ai_migrator@file-target/emmet4930_fixture\n')
    monkeypatch.setenv('MIGRATION_DATABASE_URL_FILE', str(path))
    monkeypatch.setenv('MIGRATION_DATABASE_URL', 'postgresql://ai_migrator@wrong-target/emmet4930_fixture')
    assert migration_url() == 'postgresql://ai_migrator@file-target/emmet4930_fixture'


@pytest.mark.parametrize('kind', ['empty', 'missing'])
def test_explicit_bad_migration_file_never_falls_back(tmp_path, monkeypatch, kind):
    path = tmp_path / 'dsn'
    if kind == 'empty':
        path.write_text(' \n')
    monkeypatch.setenv('MIGRATION_DATABASE_URL_FILE', str(path))
    monkeypatch.setenv('MIGRATION_DATABASE_URL', 'postgresql://ai_migrator@wrong-target/emmet4930_fixture')
    with pytest.raises((ValueError, FileNotFoundError)):
        migration_url()


def test_migration_environment_fallback(monkeypatch):
    monkeypatch.delenv('MIGRATION_DATABASE_URL_FILE', raising=False)
    monkeypatch.setenv('MIGRATION_DATABASE_URL', ' postgresql://ai_migrator@env-target/emmet4930_fixture ')
    assert migration_url() == 'postgresql://ai_migrator@env-target/emmet4930_fixture'
    monkeypatch.delenv('MIGRATION_DATABASE_URL')
    with pytest.raises(ValueError):
        migration_url()


@pytest.mark.parametrize('name', ['ai_db_develop', 'ai_db_staging', 'ai_db_production'])
def test_deployment_database_names(name, monkeypatch):
    monkeypatch.delenv('INTEGRATION_SYNTHETIC_ONLY', raising=False)
    assert validate_database_name(name) == name


@pytest.mark.parametrize('name', ['', 'ai_db_stage', 'emmet4930_staging', 'AI_DB_STAGING'])
def test_unapproved_database_names_fail_closed(name, monkeypatch):
    monkeypatch.delenv('INTEGRATION_SYNTHETIC_ONLY', raising=False)
    with pytest.raises(ValueError, match='approved environment database'):
        validate_database_name(name)


def test_legacy_database_name_is_synthetic_only(monkeypatch):
    monkeypatch.setenv('INTEGRATION_SYNTHETIC_ONLY', '1')
    assert validate_database_name('emmet4930_smoke') == 'emmet4930_smoke'


@pytest.mark.parametrize('already_exists',[False,True])
def test_recipient_sql_is_applied_even_when_existing_function_reports_unchanged(monkeypatch,already_exists):
 from integration import migrate_single_server as migration,recipient_data
 statements=[];prepared=[]
 class Result:
  def fetchone(self):return ('existing_function' if already_exists else None,)
 class DB:
  def execute(self,statement):statements.append(statement);return Result()
 monkeypatch.setattr(recipient_data,'migrate',lambda db:prepared.append(db))
 db=DB();changed=migration.migrate_recipient_data(db)
 assert changed is not already_exists
 assert prepared==[db]
 assert statements[-1]==(migration.ROOT/'integration/recipient_profile.sql').read_text()
 assert 'CREATE OR REPLACE FUNCTION ai_profile.chat_recipient_snapshot' in statements[-1]
 # changed describes first existence, not whether replacement SQL ran.
