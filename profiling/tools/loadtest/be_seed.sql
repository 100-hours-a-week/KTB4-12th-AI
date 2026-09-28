-- 부하 시험용 BE 행 — 로컬 MySQL(seonjalal-mysql-local, DB gift) 전용. 스테이징·운영에 돌리지 않는다.
-- 사용자 900001~900000+@n (loadtest<k>@load.local, 더미 해시라 로그인 불가) · 비선호 대분류 1개 순환(1~10)
-- · 프로필 NONE + 먼 과거 변경 시각 → BE 스케줄러가 다음 틱에 7.6 을 보낸다.
-- 실행: docker exec -i seonjalal-mysql-local mysql -N -ugift -p<로컬값> gift < tools/loadtest/be_seed.sql
-- @n 이 1000 을 넘으면 SET SESSION cte_max_recursion_depth = <n> 을 먼저.
SET @n = 200;
SET @base = 900000;
INSERT INTO users (id, email, password, name, birth, status, is_birthday_public, is_first_login, created_at, updated_at)
WITH RECURSIVE seq (k) AS (SELECT 1 UNION ALL SELECT k + 1 FROM seq WHERE k < @n)
SELECT @base + k, CONCAT('loadtest', k, '@load.local'),
       '$2a$10$loadtest.not.a.real.hash.000000000000000000000000000',
       CONCAT('부하', k), '2000-01-01', 'ACTIVE', 0, 0, NOW(6), NOW(6)
FROM seq;
INSERT INTO user_dislike_categories (user_id, category_id, created_at, updated_at, deleted_at)
WITH RECURSIVE seq (k) AS (SELECT 1 UNION ALL SELECT k + 1 FROM seq WHERE k < @n)
SELECT @base + k, ((k - 1) % 10) + 1, NOW(6), NOW(6), NULL
FROM seq;
-- BE Clock 은 Asia/Seoul, LocalDateTime 은 변환 없이 저장 → '2020-01-01' 은 어느 시간대에서도 과거(quiet·window 모두 지남)
INSERT INTO recipient_profiles (recipient_id, profile_status, source_version, analyzed_source_version,
                                last_changed_at, window_started_at, pending_since, retry_count, created_at, updated_at)
WITH RECURSIVE seq (k) AS (SELECT 1 UNION ALL SELECT k + 1 FROM seq WHERE k < @n)
SELECT @base + k, 'NONE', 0, 0, '2020-01-01 00:00:00', '2020-01-01 00:00:00', NULL, 0, NOW(6), NOW(6)
FROM seq;
SELECT COUNT(*) AS seeded FROM recipient_profiles WHERE recipient_id BETWEEN @base + 1 AND @base + @n;
