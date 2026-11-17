from pydantic import BaseModel,Field
class Login(BaseModel): email:str; password:str
class DataInput(BaseModel): data:dict
class RunInput(BaseModel): name:str=Field(min_length=2,max_length=120); policy_id:int; period:str='全部'
class ReviewInput(BaseModel): note:str=Field(min_length=4,max_length=2000)
class ScheduleInput(BaseModel): name:str=Field(min_length=2,max_length=120); run_id:int; interval_seconds:int=Field(ge=5,le=86400)
class MergeItemInput(BaseModel):
 natural_key:str=Field(min_length=1,max_length=120); op:str='upsert'
 base:dict|None=None; base_digest:str|None=None; incoming:dict|None=None
class MergeInput(BaseModel):
 batch_key:str=Field(min_length=4,max_length=120); note:str=Field(default='',max_length=2000); items:list[MergeItemInput]=Field(min_length=1,max_length=500)
class ResolveInput(BaseModel):
 record:str|None=None; fields:dict|None=None; downtime:str|None=None
