from fastapi import APIRouter,Depends
from fastapi.responses import JSONResponse
from src.config.database import database
from src.controllers.auth import current,editor,admin
from src.schemas.requests import MergeInput,ResolveInput
from src.services.merge import (batch_dict,create_batch,detail_batch,export_bundle,land_batch,list_batches,pending_items,recheck_batch,resolve_item,snapshot_item)
router=APIRouter()


@router.get('/merge/export')
def export(db=Depends(database),u=Depends(current)): return export_bundle(db)


@router.get('/merge/batches')
def batches(db=Depends(database),u=Depends(current)):
    return [batch_dict(b) for b in list_batches(db)]


@router.get('/merge/batches/{id}')
def batch(id:int,db=Depends(database),u=Depends(current)):
    found,items=detail_batch(db,id);return batch_dict(found,items)


@router.post('/merge/batches')
def create(b:MergeInput,db=Depends(database),u=Depends(editor)):
    obj,_=create_batch(db,u,b)
    found,items=detail_batch(db,obj.id)
    return JSONResponse(batch_dict(found,items))


@router.post('/merge/batches/{id}/recheck')
def recheck(id:int,db=Depends(database),u=Depends(editor)):
    obj,changed=recheck_batch(db,u,id)
    found,items=detail_batch(db,obj.id)
    return {'batch':batch_dict(found,items),'changed':changed}


@router.post('/merge/batches/{id}/items/{item_id}/resolve')
def resolve(id:int,item_id:int,b:ResolveInput,db=Depends(database),u=Depends(admin)):
    resolve_item(db,u,id,item_id,b)
    found,items=detail_batch(db,id)
    return batch_dict(found,items)


@router.post('/merge/batches/{id}/land')
def land(id:int,db=Depends(database),u=Depends(admin)):
    obj,applied=land_batch(db,u,id)
    found,items=detail_batch(db,obj.id)
    return {'batch':batch_dict(found,items),'applied':applied}


@router.get('/merge/pending')
def pending(db=Depends(database),u=Depends(current)):
    return [snapshot_item(i) for i in pending_items(db)]
