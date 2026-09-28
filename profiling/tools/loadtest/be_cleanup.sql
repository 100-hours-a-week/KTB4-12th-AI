-- 부하 시험 BE 행 삭제 — 로컬 전용. FK 순서대로.
SET @n = 200;
SET @base = 900000;
DELETE FROM recipient_recommended_products WHERE recipient_id BETWEEN @base + 1 AND @base + @n;
DELETE FROM recipient_profiles WHERE recipient_id BETWEEN @base + 1 AND @base + @n;
DELETE FROM user_dislike_categories WHERE user_id BETWEEN @base + 1 AND @base + @n;
DELETE FROM users WHERE id BETWEEN @base + 1 AND @base + @n AND email LIKE 'loadtest%@load.local';
SELECT COUNT(*) AS remaining FROM users WHERE email LIKE 'loadtest%@load.local';   -- 0 이어야
