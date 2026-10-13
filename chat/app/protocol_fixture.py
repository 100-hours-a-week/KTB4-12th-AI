"""Same storage/lease/ownership lifecycle, isolated authenticated synthetic generation."""
from app.mock_runtime import MockRuntime,NoExternalSearch

class ProtocolFixture(MockRuntime):
    def __init__(self,primary):
        self.primary=primary
        super().__init__(primary.store,NoExternalSearch(),primary.owner)
        self.jobs,self.admissions,self.signals=primary.jobs,primary.admissions,primary.signals
        self.resource_limits=primary.resource_limits
    @property
    def owner(self):return self.primary.owner
    @owner.setter
    def owner(self,value):pass
    @property
    def healthy(self):return self.primary.healthy
    @healthy.setter
    def healthy(self,value):pass
    def require_recovery(self):self.primary.require_recovery()
