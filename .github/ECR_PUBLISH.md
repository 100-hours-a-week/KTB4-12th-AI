# main 이미지 ECR 발행

`publish-ecr.yml`은 main push에서 기존 `pr-checks.yml` 검증을 통과한 뒤
통합 Dockerfile의 `chat`, `profiler`, `search`, `migrate` 이미지를 발행한다.
개발·PR 브랜치에서는 발행하지 않으며 ECS 배포·DB 변경·유료 AI 호출은 하지 않는다.
검증 중 기존 합성 smoke의 공개 모델 다운로드는 발생할 수 있다.

## 사전 설정

- ECR private repository: 기본 `seonjalal/ai`, 태그 정책 `IMMUTABLE`.
- Repository Variable `RELEASE_ECR_ROLE_ARN`: AI main 발행 전용 OIDC Role ARN.
- 선택 변수 `RELEASE_ECR_REPOSITORY`: 기본 이름과 다른 ECR을 쓸 때 설정.
- Role 신뢰 정책: GitHub OIDC audience `sts.amazonaws.com`, 이 저장소의
  실제 OIDC subject 중 main ref만 정확히 허용. 다른 레포의 Role은 재사용하지 않는다.
- Role 권한: `ecr:GetAuthorizationToken`은 `*`, 나머지는 대상 ECR ARN에만
  `DescribeRepositories`, `ListImages`, `DescribeImages`, `BatchGetImage`,
  `GetDownloadUrlForLayer`, `BatchCheckLayerAvailability`, `InitiateLayerUpload`,
  `UploadLayerPart`, `CompleteLayerUpload`, `PutImage`.
- ECR 생성·IAM 적용·GitHub 변수 설정은 이 workflow에서 수행하지 않는다.
  위 설정이 없으면 main 발행은 실패하며 자동으로 권한을 확장하지 않는다.

## 태그와 재실행

태그는 `<target>-<전체 커밋 SHA>`다. 예: `chat-<40자리 SHA>`.
`latest`나 가변 버전 태그는 만들지 않는다. Release Please와 별도로 동작하며
main 머지 자체가 `v2.0.0` 발행이나 운영 배포를 뜻하지 않는다.
상품 동기화 작업은 Search 이미지의 명령을 바꿔 사용하므로 별도 이미지가 없다.
Console·모의 BE·smoke 이미지도 발행하지 않는다.

부분 실패 후 재실행 시 이미 존재하는 태그는 pull하여 소스 revision label을
검증하고 재사용한다. 새 이미지마다 tag와 Digest를 job summary에 기록한다.
스테이징 배포에서는 이 Digest를 선택해 고정한다. 별도 모델·미디어·Secret·DB
준비가 필요하므로 이미지 발행 성공만으로 실서비스 기동이 보장되지는 않는다.

## 검토·복구

검토 대상은 workflow와 이 안내 두 파일뿐이다. 기존 Dockerfile과 검증 코드는
변경하지 않는다. 실제 GitHub OIDC/ECR 발행은 AWS 사전 설정과 main 반영 후 검증한다.
발행 문제가 생기면 workflow를 비활성화하거나 변경을 되돌리는 PR로 복구한다.
기존 이미지 삭제·태그 덮어쓰기·DB downgrade는 수행하지 않는다.
