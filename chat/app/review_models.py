from typing import Literal,Annotated
from pydantic import Field,UUID4,model_validator
from app.models import StrictModel
Hash=str
class SyntheticReproduction(StrictModel):
 synthetic:Literal[True]
 fixture_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
 description:str=Field(min_length=3,max_length=1200)
class RegressionProof(StrictModel):
 passed:Literal[True]
 artifact_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
 tests:list[Annotated[str,Field(min_length=1,max_length=200)]]=Field(min_length=1,max_length=30)
class ResolutionEvidence(StrictModel):
 passed:Literal[True]
 report_ids:list[UUID4]=Field(min_length=1,max_length=20)
 diagnosis:str=Field(min_length=3,max_length=1200)
 reproduction:SyntheticReproduction
 regression:RegressionProof
 revision:str=Field(pattern=r'^[a-f0-9]{16}$')
class ReportReview(StrictModel):
 review_id:UUID4
 status:Literal['pending','triaged','resolved']
 note:str=Field(min_length=3,max_length=1500)
 evidence:ResolutionEvidence|None=None
 @model_validator(mode='after')
 def resolved_proof(self):
  if self.status=='resolved' and self.evidence is None:raise ValueError('Resolved requires synthetic reproduction and passing regression evidence')
  return self
