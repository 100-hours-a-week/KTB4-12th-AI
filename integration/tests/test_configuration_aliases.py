"""Synthetic configuration/auth contracts. No AWS, provider calls, DB or containers."""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from integration.provider_key import ProviderKey, KeyUnavailable, KINDS, LEGACY_KINDS
from integration.containers import entrypoint
from integration.service_token import matches


def key(label):
    return 'sk-or-v1-' + ('synthetic_' + label + '_') * 3


@pytest.mark.parametrize('kind', ['chat', 'profiler'])
@pytest.mark.parametrize('family', ['current', 'legacy', 'equal'])
@pytest.mark.parametrize('file', [False, True])
def test_provider_aliases(kind, family, file, tmp_path):
    value = key(kind)
    names = [KINDS[kind]] if family == 'current' else [LEGACY_KINDS[kind]] if family == 'legacy' else [KINDS[kind], LEGACY_KINDS[kind]]
    env = {}
    for i, name in enumerate(names):
        if file:
            path = tmp_path / str(i); path.write_text(value + '\n'); env[name + '_FILE'] = str(path)
        else: env[name] = value
    assert ProviderKey.from_environment(kind, env).snapshot().value == value


@pytest.mark.parametrize('kind', ['chat', 'profiler'])
@pytest.mark.parametrize('file', [False, True])
def test_conflicting_alias_fails_closed(kind, file, tmp_path):
    env = {KINDS[kind]: key('new'), LEGACY_KINDS[kind]: key('old')}
    if file:
        path = tmp_path/'key'; path.write_text(key('file'))
        env[KINDS[kind] + '_FILE'] = str(path)
    with pytest.raises(KeyUnavailable, match='SOURCE_CONFLICT'):
        ProviderKey.from_environment(kind, env).snapshot()


@pytest.mark.parametrize('kind', ['chat', 'profiler'])
@pytest.mark.parametrize('blank', ['', ' ', '\n'])
def test_blank_placeholder_and_missing(kind, blank):
    env = {KINDS[kind]: blank, LEGACY_KINDS[kind]: key('old')}
    assert ProviderKey.from_environment(kind, env).snapshot().value == key('old')
    env[LEGACY_KINDS[kind]] = blank
    with pytest.raises(KeyUnavailable, match='MISSING'):
        ProviderKey.from_environment(kind, env).snapshot()


@pytest.mark.parametrize('kind', ['chat', 'profiler'])
@pytest.mark.parametrize('state', ['missing', 'empty'])
def test_explicit_file_never_falls_back(kind, state, tmp_path):
    path = tmp_path/'key'
    if state == 'empty': path.write_text(' \n')
    env = {KINDS[kind] + '_FILE': str(path), LEGACY_KINDS[kind]: key('old')}
    with pytest.raises(KeyUnavailable): ProviderKey.from_environment(kind, env).snapshot()


@pytest.mark.parametrize('kind', ['chat', 'profiler'])
@pytest.mark.parametrize('environment', ['develop', 'production'])
def test_dashboard_overrides_seed_and_stays_environment_scoped(kind, environment, tmp_path):
    tmp_path.chmod(0o700)
    env = {'OPENROUTER_MANAGED_KEY_DIR':str(tmp_path), 'AI_ENVIRONMENT':environment, KINDS[kind]:key('seed')}
    source = ProviderKey.from_environment(kind, env)
    assert source.snapshot().value == key('seed')
    saved = source.save(key('saved'), None)
    env.update({KINDS[kind] + '_FILE': str(tmp_path/'absent'), LEGACY_KINDS[kind]:key('stale')})
    reader = ProviderKey.from_environment(kind, env)
    assert reader.snapshot().value == key('saved')
    reader.save(key('replaced'), saved['revision'])
    assert source.snapshot().value == key('replaced')
    other_env = dict(env, AI_ENVIRONMENT='production' if environment == 'develop' else 'develop')
    with pytest.raises(KeyUnavailable): ProviderKey.from_environment(kind, other_env).snapshot()
    (tmp_path/source.record_file).unlink()
    with pytest.raises(KeyUnavailable): reader.snapshot()


def injected():
    return {'AI_ENVIRONMENT':'develop', 'BE_SERVICE_TOKEN':'synthetic-be-token-123456789',
            'AI_INTERNAL_SERVICE_TOKEN':'synthetic-internal-token-123456789',
            'OPENROUTER_API_KEY_CHAT':key('chat'), 'OPENROUTER_API_KEY_PROFILER':key('profiler'),
            'AI_BUDGET_MODE':'provider-key-limits', 'DATABASE_URL':'synthetic-db',
            'PROFILING_DATABASE_URL':'synthetic-db', 'SEARCH_CATALOG_DATABASE_URL':'synthetic-db',
            'SESSION_SECRET':'synthetic-session', 'QA_ADMIN_PASSWORD':'synthetic-admin', 'DEMO_ACCESS_TOKEN':'synthetic-demo'}


def configured(role, env):
    with patch.dict(os.environ,env,clear=True), patch.object(entrypoint,'database',side_effect=lambda value,role:value), patch.object(entrypoint,'profile_database'), patch.object(entrypoint,'prepare_encoder'):
        entrypoint.configure(role)
        if role in ('chat','profiler'):
            assert matches(role, 'Bearer ' + env['BE_SERVICE_TOKEN'])
            assert not matches(role, 'Bearer ' + env['AI_INTERNAL_SERVICE_TOKEN'])
            assert ProviderKey.from_environment(role).snapshot().value == key(role)
        return dict(os.environ)


@pytest.mark.parametrize('role',['chat','profiler','search'])
@pytest.mark.parametrize('file',[False,True])
def test_same_canonical_bundle_maps_roles_and_keeps_be_boundary(role,file,tmp_path):
    env=injected()
    if file:
        for name in ('AI_INTERNAL_SERVICE_TOKEN','OPENROUTER_API_KEY_CHAT','OPENROUTER_API_KEY_PROFILER'):
            path=tmp_path/name;path.write_text(env[name]); env[name+'_FILE']=str(path)
            if name != 'AI_INTERNAL_SERVICE_TOKEN':del env[name]
    result=configured(role,env)
    destination={'chat':'SEARCH_TOKEN','profiler':'PROFILING_SEARCH_TOKEN','search':'SEARCH_SERVICE_TOKEN'}[role]
    assert result[destination] == env['AI_INTERNAL_SERVICE_TOKEN']


@pytest.mark.parametrize('role',['chat','profiler'])
def test_legacy_be_conflict_still_rejected(role):
    env=injected();env['AI_SERVICE_TOKEN']='synthetic-internal-token-123456789'
    with pytest.raises(KeyUnavailable,match='SERVICE_TOKEN_SOURCE_CONFLICT'):configured(role,env)


@pytest.mark.parametrize('file',[False,True])
def test_search_old_internal_name_still_works(file,tmp_path):
    env=injected();value=env.pop('AI_INTERNAL_SERVICE_TOKEN')
    if file:
        path=tmp_path/'old';path.write_text(value);env['AI_SERVICE_TOKEN_FILE']=str(path)
    else:env['AI_SERVICE_TOKEN']=value
    assert configured('search',env)['SEARCH_SERVICE_TOKEN']==value


def test_search_alias_conflict_and_missing_fail_closed():
    env=injected();env['AI_SERVICE_TOKEN']='synthetic-other'
    with pytest.raises(ValueError,match='INTERNAL_TOKEN_SOURCE_CONFLICT'):configured('search',env)
    env.pop('AI_SERVICE_TOKEN');env.pop('AI_INTERNAL_SERVICE_TOKEN')
    with pytest.raises(ValueError,match='AI_INTERNAL_SERVICE_TOKEN'):configured('search',env)


@pytest.mark.parametrize('role',['chat','profiler'])
def test_canonical_file_rotation_does_not_create_stale_legacy_alias(role,tmp_path):
    env=injected();name=KINDS[role];path=tmp_path/'key';path.write_text(key(role))
    env.pop(name);env[name+'_FILE']=str(path)
    result=configured(role,env)
    assert LEGACY_KINDS[role] not in result
    path.write_text(key('rotated'))
    assert ProviderKey.from_environment(role,result).snapshot().value==key('rotated')


def test_search_auth_uses_internal_and_rejects_be_token(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'product-search'))
    from search.common_api import authorize
    env=injected();result=configured('search',env)
    with patch.dict(os.environ,result,clear=True):
        authorize(SimpleNamespace(headers={'authorization':'Bearer '+env['AI_INTERNAL_SERVICE_TOKEN']}),required=True)
        with pytest.raises(HTTPException) as error:
            authorize(SimpleNamespace(headers={'authorization':'Bearer '+env['BE_SERVICE_TOKEN']}),required=True)
        assert error.value.status_code==401


def test_managed_compose_internal_file_names_are_consistent():
    import yaml
    path=Path(__file__).resolve().parents[1]/'containers/compose.yaml'
    config=yaml.safe_load(path.read_text())
    for role in ('chat','profiler','search'):
        env=config['services'][role]['environment']
        assert 'AI_INTERNAL_SERVICE_TOKEN' in env and 'AI_SERVICE_TOKEN_FILE' not in env



@pytest.mark.parametrize('role',['chat','profiler','search'])
@pytest.mark.parametrize('blank',['','   '])
def test_internal_missing_or_blank_never_uses_be_token(role,blank):
    env=injected();env['AI_INTERNAL_SERVICE_TOKEN']=blank
    with pytest.raises(ValueError,match='AI_INTERNAL_SERVICE_TOKEN'):configured(role,env)


def test_search_equal_aliases_are_safe():
    env=injected();env['AI_SERVICE_TOKEN']=env['AI_INTERNAL_SERVICE_TOKEN']
    assert configured('search',env)['SEARCH_SERVICE_TOKEN']==env['AI_INTERNAL_SERVICE_TOKEN']


def test_search_configured_empty_canonical_file_never_uses_old_token(tmp_path):
    env=injected();path=tmp_path/'empty';path.write_text(' \n')
    env['AI_INTERNAL_SERVICE_TOKEN_FILE']=str(path);env['AI_SERVICE_TOKEN']=env['AI_INTERNAL_SERVICE_TOKEN']
    with pytest.raises(ValueError,match='empty'):configured('search',env)
