# develop 통합 후보 검토표

2026-10-17 PR15 squash 병합 후 `develop` `bfce5219ef622a1634569e3b92b879d10e51e9b2`에서 분기한 후보다. 동결 후보 `f8821e6b593b7cc751e064ea46ca6c9202da6bde`(tree `130faaa0c354a3b159f04046c4591627f4fbf94b`)를 수정하지 않고 파일별 차이를 새 브랜치에 적용했다. 최신 개인정보 로그 보호와 S0 순위 계산 후 비선호 제외를 보존했다.

PC 기준은 `main` `1ea3cdefe3137503be6725651452f0a25f0a1892` + 당시 미커밋 스냅샷의 `stable-v4.12`다. workbench와 Search의 기존 검색 의미를 보존했다. 개인 대화·키·학습 산출물, 과거 비공개 문서·ID 대조표를 포함하지 않는다. 모델·DB·로그·검증 산출물은 Git과 build context 밖에 둔다.

## 검증과 공개 범위

현재 무료 회귀는 Python **1,289개**(Chat 360, Profiler 434, Search 277, 통합·Console 218)와 JS **46개**가 통과했다. Profiler 434개에는 기존 develop **397개**와 추가 경계 회귀 37개가 포함되며 기존 unit 파일은 바꾸지 않았다. 오프라인 26개 skip은 별도 PostgreSQL·CPU 실행이 필요한 24개와 Chat `TEST_DATABASE_URL` 조건부 시험 1개, upstream v3 Search 계약 합의 대기 1개다.

새 임시 PostgreSQL에서 fresh·0010→0011, 제한 역할, CPU 검색·HTTP/SSE·재시작·백업/복원 24개도 통과했다. 따라서 독립 실행을 포함한 Python 통과 수는 1,313개이며 조건부 skip은 2개 남는다. 시험용 클러스터는 종료했다. 잠금 파일·소스 감사 결과는 후보 외부의 `final-result.json`, `verification/`에 기록한다. 각 실행 명령, JUnit, skip 사유, 파일별 포팅 판단, source manifest와 develop 기준 patch를 함께 제공한다. CI 설정은 추가했으나 GitHub CI를 실행한 것은 아니다.

uv lock 5개 offline check, 소스 434개 경계 검사, Python 구문·미정의 이름 검사는 통과했다. 동결 후보 소스 432개와 기존 증거 80개의 SHA256도 유지된다. Compose config는 로컬 Docker의 Compose 명령 부재로 실행되지 않았다. 설치하거나 실행기를 새로 시작하지 않았다.

이전 후보의 Apple Linux/arm64 이미지·CPU 기동·유료 합성 결과는 해당 소스에 대한 과거 증거다. 이번 최종 tree의 Linux 이미지 빌드, 전체 Compose 기동, amd64, 실제 BE 연결, 최종 tree LLM 실행은 검증하지 않았다. 기존 컨테이너·데모·DB·비용 원장을 변경하지 않는다.

짧은 Draft PR 초안을 별도 준비하되 최종 SHA·diff·본문의 독립 검토 전 push나 PR 게시를 하지 않는다. 이 브랜치는 develop 통합용이며 main 변경·merge·배포를 실행하지 않았다.

## 명시적으로 제외한 정책 변경

[에멧 변경 기록](EMMET_PR15_CHANGES.md)에 동결 후보와의 차이를 모두 기록한다. 장문은 마스킹 후 앞 1,000자 사용, 저평점 글만으로 추가 LLM 호출 없음, native Profiler 임베딩 장애의 기존 fallback, 원래 어휘와 태그 지문을 유지한다. 통합 실행만 필수 임베딩 실패 시 결과 저장을 막고 shared taxonomy를 검사한다. 이 차이를 native 정책 변경과 혼동하지 않는다.

**BE400의 하루 10턴·취향 수정 3크레딧은 BE 소유의 미구현 계획**이다. AI에 사용량 정책을 구현하지 않았고 기존 예산 예약·미확정 상태 보존을 유지한다.

## 남은 연결·운영 결정

| 경계 | 아직 필요한 내용 | 현재 동작 |
|---|---|---|
| BE export | 상품/categories 동일 snapshot UUID·complete·count·digest 및 삭제 복원 계약 | 합성 계약만 구현·검증. 완전성 없는 실제 응답을 연결하지 않음 |
| 프로필 읽기 | BE의 caller/recipient/sourceVersion grant 발행·만료·철회 | service token만으로 소유권 생성 안 함. 외부 Chat API에 자동 연결 안 함 |
| 저장 태그 | 추론 당시 taxonomy 증거의 저장 계약 | 과거 `tagTaxonomy=null`. 미검증 추론 태그를 임의 보충하지 않음 |
| 사전 계산 파일 | 정확한 파일 링크·manifest·pinned 생성 코드 | 이전 읽기 검색에서 파일 미식별. 이번에 실제 상품 파일을 복사·생성하지 않음 |
| 세대 전환 | 양쪽 소비자의 접수·claim·미완료 작업을 닫는 운영 절차 | snapshot/binding 보호. 자동 무중단 encoder 전환은 미구현 |
| 배포 | Compose 검증 호스트·대상 CPU·준비 DB/역할/볼륨/secret 파일 | 최신 전체 Compose·amd64 검증 없음. 컨테이너 실행기를 새로 가동하지 않음 |
| 모델 사용 조건 | model.lock.json에 기록된 라이선스와 배포 용도의 확인 | 기존 모델·검색 의미 유지. 사용 조건 확인 완료로 주장하지 않음 |
| 비용 | 기존 누적 gateway/원장의 운영과 미확정 예약 처리 | 한도 증가·계정 초기화·미확정 자동 환급 없음 |

이전 후보에서 확인한 REST API 자료의 generatedAt/products·snapshot 설명만으로 이 후보가 요구하는 UUID·count·digest envelope나 grant 발행을 구현했다고 보지 않는다. 실제 BE export와 grant 연결, 사전 계산 실물의 호환 판정은 미완료다. BE·클라우드 저장소에 쓰기 작업을 하지 않았다.
