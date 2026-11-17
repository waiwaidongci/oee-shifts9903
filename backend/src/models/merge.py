from sqlalchemy import Column,Integer,String,JSON,Text,Float,Index
from src.config.database import Base


class MergeLock(Base):
    """单行互斥锁：串行化批次的落地与裁决，核算读取不受影响。"""

    __tablename__ = 'draft_merge_locks'
    id = Column(Integer, primary_key=True)


class MergeBatch(Base):
    __tablename__ = 'draft_merge_batches'
    id = Column(Integer, primary_key=True)
    batch_key = Column(String(120), unique=True, nullable=False)
    actor = Column(String(120))
    created = Column(Float)
    status = Column(String(20), default='pending')
    payload_digest = Column(String(64))
    request = Column(JSON)
    summary = Column(JSON)
    note = Column(Text, default='')
    landed_at = Column(Float)
    revalidated_at = Column(Float)


class MergeItem(Base):
    __tablename__ = 'draft_merge_items'
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, index=True, nullable=False)
    ordinal = Column(Integer)
    natural_key = Column(String(160), index=True, nullable=False)
    op = Column(String(16))
    base = Column(JSON)
    base_digest = Column(String(64))
    incoming = Column(JSON)
    server = Column(JSON)
    server_digest = Column(String(64))
    result = Column(JSON)
    status = Column(String(20))
    conflict = Column(JSON)
    message = Column(Text)
    applied_id = Column(Integer)
    checked_at = Column(Float)


Index('ix_draft_merge_items_batch_ordinal', MergeItem.batch_id, MergeItem.ordinal)
