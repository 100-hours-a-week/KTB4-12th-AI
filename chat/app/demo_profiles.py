"""Fictional recipient fixtures; only the authenticated test UI can bind these."""
from copy import deepcopy
from app.profile import RecipientProfile

PROFILES = {}
for key,ident,label,liked,disliked in [
 ('mother',900000101,'어머니',['차','산책','실용적인 생활용품'],['커피','강한 향']),
 ('father',900000102,'아버지',['커피','등산','실용적인 소품'],['단 간식','강한 향']),
 ('minji',900000103,'민지 · 친구',['커피','무가당','책상용품'],['향수','디퓨저']),
 ('junho',900000104,'준호 · 친구',['게임','책상용품'],['식품','향 제품']),
]:
 profile=RecipientProfile(recipientUserId=ident,sourceVersion=1,profileStatus='COMPLETED',preferredTags=liked,dislikedTags=disliked,recommendedProductIds=[])
 PROFILES[key]={'key':key,'label':label,'source':'TEST_FIXTURE','status':'COMPLETED','data':profile.model_dump()}

def demo_profile(key):
 if key=='none':return None
 if key not in PROFILES:
  from app.errors import APIError
  raise APIError(400,'목록에서 테스트 수신자를 선택해 주세요.')
 return deepcopy(PROFILES[key])

def demo_profiles():return [deepcopy(p) for p in PROFILES.values()]
