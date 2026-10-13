"""Direct PostgreSQL adapter for deployments without the HF storage bridge."""
import asyncio
from app.store import Store
class LocalStore:
 def __init__(self,url):self.store=Store(url)
 async def call(self,method,**args):return await asyncio.to_thread(getattr(self.store,method),**args)
 async def close(self):await asyncio.to_thread(self.store.close)
