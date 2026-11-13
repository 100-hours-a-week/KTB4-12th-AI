# Changelog

## 1.0.0 (2026-11-13)


### Features

* **admin:** keep Modal keys transient and activation explicit ([2736fda](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/2736fda44ab085ae2401380ce5a41fd168f75d5f))
* **admin:** use transient Modal inference keys ([#42](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/42)) ([2736fda](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/2736fda44ab085ae2401380ce5a41fd168f75d5f))
* AI v2 프로파일링 · 공용 카탈로그 · 서버 배포 설정 ([#34](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/34)) ([21b83ac](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/21b83ac1c288d3c8e1c3336ec8a2537a623cc97b))
* Chat·Profiler·Search 공용 카탈로그 통합 ([#16](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/16)) ([472d8f2](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/472d8f2a65a5624c2854ca59b4c1133ffc47ab13))
* **chat:** promote dashboard and model connection improvements to main ([124e074](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/124e074987ab7e9cf3e5fb9796e21636e55c1878))
* **integration:** align internal token and provider key aliases ([#23](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/23)) ([0961cd0](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/0961cd04e0323fa4c33afdf9934c3d582656a734))
* **integration:** ECS 서버 설정 — DB 호스트 · 비밀값 환경변수 · 비용 한도 모드 · 모델 설정 ([#19](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/19)) ([21b83ac](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/21b83ac1c288d3c8e1c3336ec8a2537a623cc97b))
* **integration:** ECS 서버 설정 — DB 호스트 · 비밀값 환경변수 · 비용 한도 모드 · 모델 설정 ([#19](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/19)) ([06c0a9c](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/06c0a9cca9cab9f5e84ae0cbc5546f79f11f708a))
* **integration:** ECS 서버에서 뜨도록 로컬 전용 설정 정리 ([#20](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/20)) ([06c0a9c](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/06c0a9cca9cab9f5e84ae0cbc5546f79f11f708a))
* **integration:** 서비스 키별 한도와 Console 실행·진단 개선 ([#22](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/22)) ([bb2406a](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/bb2406a464fd00b8cdb65b3315046ec2e9b582a4))
* **product-search:** 상품 적재 S3 묶음 — 서버 임베딩 없이 ([#25](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/25)) ([d56e53b](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/d56e53bf59d39ed466560429ffe38082c405ab4d)), closes [#24](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/24)
* **profiler-v3/pipeline:** [프로파일러] BE ↔ AI 프로파일링 통신 HTTP 비동기 → RabbitMQ ([#54](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/54)) ([6682484](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/66824847d1e24f617acb6d6543e00c1f241afbc0))
* **profiler-v3/pipeline:** [프로파일러] v3 재현 개선 — 인젝션 문장만 제거 · 낮은 별점 비선호 호출 · 비선호 어휘 물러남 ([#56](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/56)) ([ff06902](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/ff06902b4b9675ab657ebb3c01129d82edb1c4c8))
* **profiler-v3/pipeline:** [프로파일러] v3 태그 엔진(Modal) 연결 · v2 폴백 ([#47](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/47)) ([b9a111f](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/b9a111f82f05bac0718edf986e24d83810f70dd5))
* **profiling:** 프로파일링 v2 적용 ([#15](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/15)) ([bfce521](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/bfce5219ef622a1634569e3b92b879d10e51e9b2))


### Bug Fixes

* **ai:** bound embedding batches and isolate staging credentials ([#50](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/50)) ([2a29f32](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/2a29f32037ccb23bfe6db7b2a2fa874c905049f7))
* **ai:** 저장 취향을 채팅에 연결 — develop 변경을 main 에 반영 ([bd0f057](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/bd0f057929a04c88f777061ce9d0ea52ea65a5f1))
* **ai:** 저장 취향을 채팅에 연결하고 Budget Gateway 제거 ([#36](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/36)) ([3d07f0e](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/3d07f0e6b8176088b83f5f267e5933389f38dba9))
* **chat:** bound preflight database admission ([#51](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/51)) ([22e73a6](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/22e73a622fae09e61c33e2dcb6817a189e3959bd))
* **chat:** expose actionable early failure diagnostics ([97401e2](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/97401e2dd0dfe659a6580b82144430a4eae09f65))
* **chat:** guide gift intent and expose timeout diagnostics ([a8304dc](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/a8304dc66b884695ca20af738ffda5c4e743a55f))
* **chat:** guide gift intent and expose timeout diagnostics ([#60](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/60)) ([a8304dc](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/a8304dc66b884695ca20af738ffda5c4e743a55f))
* **chat:** improve diagnostics, debug records and recommendations ([#63](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/63)) ([d1d6dca](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/d1d6dca960afcc2e63778408b5ebfdad026becf4))
* **chat:** preserve model-authored recommendation reasons ([#40](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/40)) ([e738348](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/e73834886010bfd10fd14d8b686773a9f63d7515))
* **chat:** 선물 상담 QA 및 진단 개선 main 반영 ([0773c10](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/0773c107d66ead5ad5b71d4bcf0634f7830e53f6))
* **chat:** 진단과 관리자 기록 개선 main 반영 ([d714d93](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/d714d93005cc96636315c6e2a234ac10014382e0))
* **chat:** 진단과 관리자 기록 개선 main 반영 ([#64](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/64)) ([d714d93](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/d714d93005cc96636315c6e2a234ac10014382e0))
* **chat:** 초기 실패 원인과 처리 흐름을 대시보드에 표시 ([64e0a89](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/64e0a89454c9346ebd88bd308d4896756309a9a6))
* **ci:** initialize Docker config in a runner step ([#31](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/31)) ([7ded367](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/7ded367df33a7a43f3a6fc3d3ece2e4bee8d77dc))
* **integration:** AI 환경별 DB 이름 규칙 — CI · smoke · 시험 맞춤 ([#28](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/28)) ([2445c12](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/2445c12d8f2ab41df93212b059a4be995a4cb9cb))
* **profiler:** promote Modal client safety fixes to main ([e762634](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/e762634a7b7b16fec3ac9598a3f00ca79e843f10))
* **profiler:** secure Modal redirects and preserve result retries ([#58](https://github.com/100-hours-a-week/KTB4-12th-AI/issues/58)) ([0a46dcb](https://github.com/100-hours-a-week/KTB4-12th-AI/commit/0a46dcbd299286b18c74e97686ef07f2099b3c82))
