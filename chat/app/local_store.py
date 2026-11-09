"""Bounded PostgreSQL dispatch with reserved execution and recovery capacity."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from app.store import Store
from app.resource_limits import PREFLIGHT_STORAGE, ResourceLimits


class LocalStore:
 def __init__(self,url):
  self.store=Store(url)
  self._init_dispatch()

 def _init_dispatch(self):
  # Sum of workers equals Store's six connections. Queues are bounded as well.
  # Preflight cannot borrow generation, user recovery, or lease capacity.
  self._lanes={name:{'executor':ThreadPoolExecutor(max_workers=workers,thread_name_prefix='chat-db-'+name),
                     'pending':0,'limit':limit}
               for name,workers,limit in [('preflight',1,8),('execution',2,8),('control',1,32),('lease',2,2)]}
  self._limits=ResourceLimits()
  self._closed=False

 async def call(self,method,**args):
  # Also supports adapters assembled with __new__ by isolated pool tests.
  if not hasattr(self,'_lanes'):self._init_dispatch()
  if self._closed:raise self._limits.unavailable()
  if method=='lease':name='lease'
  elif PREFLIGHT_STORAGE.get() or method=='existing_turn':name='preflight'
  elif method in {'binding','owned','session','read','replay','health'} or (method=='context' and 'value' not in args) or (method=='emit' and args.get('payload',{}).get('status')=='CANCELLED'):name='control'
  else:name='execution'
  lane=self._lanes[name]
  if lane['pending']>=lane['limit']:raise self._limits.unavailable()
  lane['pending']+=1
  try:
   context=copy_context()
   future=asyncio.get_running_loop().run_in_executor(lane['executor'],lambda:context.run(getattr(self.store,method),**args))
  except BaseException:
   lane['pending']-=1
   raise
  def completed(done):
   lane['pending']-=1
   if not done.cancelled():done.exception()
  future.add_done_callback(completed)
  # Shield the actual worker future, not only the caller: cancelling a request
  # must neither cancel queued DB work nor release capacity for a live thread.
  return await asyncio.shield(future)

 async def close(self):
  if not hasattr(self,'_lanes'):self._init_dispatch()
  self._closed=True
  try:
   # Pool close must not queue behind a blocked lease/database worker.
   await asyncio.to_thread(self.store.close)
  finally:
   for lane in self._lanes.values():lane['executor'].shutdown(wait=False)
