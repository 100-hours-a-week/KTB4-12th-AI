"""태그 규격(TagSpec) — 축 · 프롬프트 · 스키마 · 판 이름이 한 묶음으로 바뀌는가, 어긋나면 거부하는가(10-13)."""

import pydantic
import pytest

from profiling import pipeline, tagging
from profiling.settings import Settings
from profiling.types import RunStatus
from tests.unit.test_tagging import FakeTagger, _p, _profile, _rq


def test_default_spec_is_tag4_and_keeps_the_10_12_version() -> None:
    sp = tagging.load_spec("tag4")
    assert sp.axes == tagging.ALL_AXES and sp.prompt_file == tagging.DEFAULT_PROMPT_FILE
    assert sp.version == "tag4-2fc950d4" and sp.validator_version == pipeline.VALIDATOR_VERSION_TAG4   # 10-12 비교 실험 · 기존 기록과 같은 값
    assert tagging.SPEC.name == "tag4" and tagging.PROMPT_VERSION == sp.version                        # 설정 없으면 기본 규격


@pytest.mark.parametrize("name, n", [("tag4", 4), ("tag3", 3)])
def test_spec_moves_axes_prompt_schema_and_version_together(name, n) -> None:
    sp = tagging.load_spec(name)
    assert len(sp.axes) == n and sp.prompt_file.name == f"{name}_system.md"
    assert sp.schema["properties"]["tags"]["items"]["properties"]["axis"]["enum"] == list(sp.axes)     # LLM 출력이 이 축만 쓰게
    assert sp.version.startswith(f"{name}-") and sp.validator_version == f"{name}-v1"
    assert tagging.declared_axes(sp.system) == set(sp.axes)                                            # 프롬프트에 적힌 축 = 규격의 축


def test_tag3_has_no_feature_axis_and_a_different_version() -> None:
    t3, t4 = tagging.load_spec("tag3"), tagging.load_spec("tag4")
    assert "특징" not in t3.axes and "특징" not in tagging.declared_axes(t3.system)
    assert t3.version != t4.version and t3.version == "tag3-56098b49"                                 # 10-13 3축 시험과 같은 판


@pytest.mark.parametrize("name, other", [("tag3", "tag4"), ("tag4", "tag3")])
def test_mismatched_prompt_file_is_refused(name, other) -> None:
    with pytest.raises(ValueError, match="축"):                       # 3축 규격 + 4축 프롬프트(또는 반대) — 조용히 돌지 않고 거부
        tagging.load_spec(name, tagging.PROMPT_DIR / f"{other}_system.md")


def test_prompt_override_keeps_the_spec_axes(tmp_path) -> None:
    alt = tmp_path / "tag3_alt.md"
    alt.write_text(tagging.load_spec("tag3").system + "\n8. 막연한 말('실용적인 선물')은 카테고리가 아니다.\n", encoding="utf-8")
    sp = tagging.load_spec("tag3", alt)                                 # 문구만 바꾼 실험 — 축은 규격대로, 판 이름만 바뀐다
    assert sp.axes == tagging.SPEC_AXES["tag3"] and sp.version.startswith("tag3-") and sp.version != "tag3-56098b49"


def test_unknown_spec_name_is_refused_by_loader_and_settings() -> None:
    with pytest.raises(ValueError, match="모르는 태그 규격"):
        tagging.load_spec("tag5")
    with pytest.raises(pydantic.ValidationError):
        Settings(TAG_SPEC="tag5")                                       # PROFILING_TAG_SPEC=tag5 → 기동 실패


def test_validate_follows_the_spec_axes() -> None:
    tags = [{"axis": "특징", "tag": "은은한 향", "ref": "pref", "evidence": "x"},
            {"axis": "카테고리", "tag": "향수", "ref": "pref", "evidence": "x"}]
    keep4, drop4 = tagging.validate(tags, {}, True, tagging.load_spec("tag4"))
    keep3, drop3 = tagging.validate(tags, {}, True, tagging.load_spec("tag3"))
    assert [t["tag"] for t in keep4] == ["은은한 향", "향수"] and not drop4
    assert [t["tag"] for t in keep3] == ["향수"] and [(t["tag"], t["drop"]) for t in drop3] == [("은은한 향", "축 밖")]


def test_profile_uses_the_passed_spec_for_system_schema_and_versions() -> None:
    seen = {}

    class Spy(FakeTagger):
        def extract(self, system, prompt, schema):
            seen["system"], seen["enum"] = system, schema["properties"]["tags"]["items"]["properties"]["axis"]["enum"]
            return super().extract(system, prompt, schema)

    products = [_p(i, 1, 100 - i) for i in range(1, 21)]
    tagger = Spy([{"axis": "카테고리", "tag": "향수", "ref": "pref", "evidence": "x"},
                  {"axis": "특징", "tag": "은은한 향", "ref": "pref", "evidence": "x"}])
    t3 = tagging.load_spec("tag3")
    out, _ = _profile(_rq(pref="향수 좋아요"), products, tagger=tagger, tag_spec=t3)
    assert out.status is RunStatus.RESULT_READY
    assert seen["system"] == t3.system and seen["enum"] == ["카테고리", "IP", "브랜드"]       # 서비스가 넘긴 규격 그대로 LLM 에
    assert out.prompt_version == f"{t3.version}@fake-llm" and out.validator_version == "tag3-v1"
    assert out.validation.preferred_tags == ["향수"] and out.validation.key_features == []      # 특징은 축 밖으로 버려짐
