-- Chat reads only the recipient of an existing trusted conversation. No raw
-- profile table grant, caller-supplied recipient ID, HTTP lookup or BE round trip.
-- The existing service-authenticated BE conversation is the authority. This
-- function accepts only its immutable conversation UUID, not a recipient ID;
-- PUBLIC has no execute permission and only ai_chat_service may call it.
CREATE OR REPLACE FUNCTION ai_profile.chat_recipient_snapshot(conversation_id uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
  WITH bound AS (
    SELECT c.sender,c.recipient FROM ai_chat.conversations c
    WHERE c.id = conversation_id AND c.demo_profile IS NULL
      AND NOT c.protocol_fixture
      AND NOT EXISTS (SELECT 1 FROM ai_chat.sessions s WHERE s.sender = c.sender)
  ), profile AS (
    SELECT i.*, r.status AS run_status, r.tag_status AS run_tag_status,
      (i.successful_analysis->>'sourceVersion')::bigint AS analyzed_version
    FROM bound b JOIN ai_profile.recipient_inputs i ON i.recipient_user_id = b.recipient
    LEFT JOIN ai_profile.profile_runs r ON r.recipient_user_id = i.recipient_user_id
      AND r.source_version = i.source_version
  )
  SELECT COALESCE((SELECT jsonb_build_object(
    'source', 'AI_PROFILE_STORE',
    'status', CASE WHEN COALESCE(btrim(guarded_input->>'giftPreference'), '') <> ''
      OR jsonb_array_length(guarded_input->'dislikedCategories') > 0
      OR (analyzed_version = source_version AND (
        jsonb_array_length(successful_analysis->'preferredTags') > 0
        OR jsonb_array_length(successful_analysis->'dislikedTags') > 0)) THEN 'AVAILABLE'
      WHEN COALESCE(btrim(input->>'giftPreference'), '') <> '' THEN 'UNAVAILABLE' ELSE 'NONE' END,
    'inputSourceVersion', source_version,
    'analysisSourceVersion', analyzed_version,
    'analysisCurrent', COALESCE(analyzed_version = source_version, false),
    'analysisStatus', CASE WHEN analyzed_version = source_version THEN 'COMPLETED'
      WHEN run_status = 'FAILED' OR run_tag_status LIKE 'fail:%'
        OR run_tag_status IN ('bad_json','empty','no_tags_list') THEN 'FAILED'
      WHEN run_tag_status = 'off' THEN 'UNAVAILABLE'
      WHEN run_status = 'RUNNING' THEN 'PROCESSING' ELSE 'PENDING' END,
    'receivedAt', received_at,
    'inputGuardVersion', guard_version,
    'data', jsonb_build_object(
      'recipientUserId', recipient_user_id,
      'sourceVersion', source_version,
      'giftPreference', guarded_input->'giftPreference',
      'dislikedCategories', guarded_input->'dislikedCategories',
      'preferredTags', CASE WHEN analyzed_version = source_version
        THEN successful_analysis->'preferredTags' ELSE '[]'::jsonb END,
      'dislikedTags', CASE WHEN analyzed_version = source_version
        THEN successful_analysis->'dislikedTags' ELSE '[]'::jsonb END
    )
  ) FROM profile), CASE
    WHEN NOT EXISTS(SELECT 1 FROM bound)
      OR NOT EXISTS(
        SELECT 1 FROM bound b JOIN ai_profile.recipient_inputs i ON i.recipient_user_id=b.recipient)
      THEN '{"source":"AI_PROFILE_STORE","status":"NOT_PROVIDED"}'::jsonb
    ELSE '{"source":"AI_PROFILE_STORE","status":"UNAVAILABLE","accessStatus":"NOT_AUTHORIZED"}'::jsonb END)
$$;
REVOKE ALL ON FUNCTION ai_profile.chat_recipient_snapshot(uuid) FROM PUBLIC;
GRANT USAGE ON SCHEMA ai_profile TO ai_chat_service;
GRANT EXECUTE ON FUNCTION ai_profile.chat_recipient_snapshot(uuid) TO ai_chat_service;
GRANT SELECT,INSERT,UPDATE ON ai_profile.recipient_inputs TO ai_profiler_service;
GRANT SELECT,INSERT ON ai_profile.recipient_input_versions TO ai_profiler_service;
REVOKE UPDATE ON ai_profile.recipient_input_versions FROM ai_profiler_service;
REVOKE DELETE ON ai_profile.recipient_inputs,ai_profile.recipient_input_versions FROM ai_profiler_service;
