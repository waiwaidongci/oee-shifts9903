import hashlib,json,time
from fastapi import HTTPException
from src.models.source import Source,Policy
from src.models.run import Run
from src.services.audit import log
from src.config.settings import REDIS_URL
import redis
def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def submit(db,user,body):
 policy=db.get(Policy,body.policy_id)
 if not policy: raise HTTPException(404,'口径版本不存在')
 # 单条查询读取全部底稿：不在合并落地过程中检查中间状态，只看到整批落地前或后的底稿。
 sources=[x.data for x in db.query(Source).order_by(Source.id).all() if body.period=='全部' or x.data.get('period')==body.period]
 if not sources: raise HTTPException(422,'所选期间没有底稿')
 snapshot={'policy':policy.data,'sources':sources,'period':body.period}
 snapshot['basis']={'sources_digest':digest(sources),'source_count':len(sources)}
 checksum=digest(snapshot)
 obj=Run(name=body.name,status='queued',snapshot=snapshot,checksum=checksum,created=time.time(),approved=False)
 db.add(obj);log(db,user.email,'run.queue',body.name);db.commit()
 redis.Redis.from_url(REDIS_URL).rpush('report-jobs',obj.id)
 return obj
def approve(db,user,id,note):
 obj=db.get(Run,id)
 if not obj: raise HTTPException(404,'报告不存在')
 if obj.status!='completed': raise HTTPException(409,'仅可复核已完成报告')
 if obj.approved: raise HTTPException(409,'报告已经复核')
 obj.approved=True;obj.review=note;log(db,user.email,'run.approve',id);db.commit();return obj
