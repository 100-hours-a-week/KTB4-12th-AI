-- 부하 시험 BE 행 되돌리기(재실행용) — 로컬 전용. source_version 은 일부러 그대로 둔다:
-- 0 으로 되돌리면 AI 가 같은 (수신자, 버전)을 재전송(RESEND)으로 판정해 분석을 다시 하지 않는다. 다음 발송은 v+1 로 나간다.
SET @n = 200;
SET @base = 900000;
DELETE FROM recipient_recommended_products WHERE recipient_id BETWEEN @base + 1 AND @base + @n;
UPDATE recipient_profiles
   SET profile_status = 'NONE', pending_since = NULL, retry_count = 0,
       last_changed_at = '2020-01-01 00:00:00', window_started_at = '2020-01-01 00:00:00', updated_at = NOW(6)
 WHERE recipient_id BETWEEN @base + 1 AND @base + @n;
SELECT profile_status, source_version, COUNT(*) FROM recipient_profiles
 WHERE recipient_id BETWEEN @base + 1 AND @base + @n GROUP BY 1, 2;
