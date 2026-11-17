import {heading} from '../components/common/header.js';
import {esc,time} from '../utils/format.js';

const STATUS={pending:'待裁决',ready:'待落地',landed:'已落地',landing:'落地中'};
const ITEM={applied:'已落地',auto:'自动合入',noop:'无变化',conflict:'待裁决',resolved:'已裁决',skipped:'跳过'};
const KIND={'field':'同一字段异改 / 停机交叠异改','delete-modify':'撤销与修改冲突','add-collision':'新增撞键冲突','constraint':'合并后业务约束不满足'};

const badge=(s,map)=>`<span class="status ${esc(s)}">${esc(map[s]||s)}</span>`;
const intervalText=list=>Array.isArray(list)&&list.length?list.map(([a,b])=>`${a}–${b}`).join('、'):'（无）';
// 字段值里的停机可能是区间数组，也可能是完整停机记录；两种都安全呈现。
const arrayText=value=>Array.isArray(value)?(value.length&&Array.isArray(value[0])?intervalText(value):esc(JSON.stringify(value))):(value===null||value===undefined?'—':esc(value));
const json=value=>value===null||value===undefined?'（撤销/无）':`<code>${esc(JSON.stringify(value))}</code>`;

function fieldValue(value,key){
  if(key==='downtime')return arrayText(value);
  if(value===null||value===undefined)return '—';
  return esc(typeof value==='object'?JSON.stringify(value):value);
}

function fieldRows(c,conflict){
  const labels=c.fields||{};
  const rows=(conflict.fields||[]).map(f=>`<tr data-field="${esc(f.field)}"><th>${esc(labels[f.field]||f.field)}</th><td>${fieldValue(f.base,f.field)}</td><td>${fieldValue(f.server,f.field)}</td><td>${fieldValue(f.incoming,f.field)}</td></tr>`).join('');
  return rows;
}

function basis(c,item){
  const conflict=item.conflict;
  if(!conflict)return '';
  const kind=KIND[conflict.kind]||conflict.kind;
  if(conflict.kind==='field'){
    const down=conflict.downtime?`<div class="conflict-block"><h4>停机修改范围</h4><p>库内修改：${intervalText(conflict.downtime.server_edits)} · 回传修改：${intervalText(conflict.downtime.incoming_edits)} · 交叠：${intervalText(conflict.downtime.overlap)}</p><p>原始依据：${intervalText(conflict.downtime.base)}</p></div>`:'';
    return `<div class="conflict"><p class="eyebrow">待裁决 · ${esc(kind)}</p><p>${esc(conflict.message)}</p><div class="table-wrap"><table><thead><tr><th>字段</th><th>原始依据</th><th>库内当前</th><th>回传修订</th></tr></thead><tbody>${fieldRows(c,conflict)}</tbody></table></div>${down}</div>`;
  }
  const sides=conflict.kind==='constraint'
    ? `<div class="table-wrap"><table><thead><tr><th>字段</th><th>原始依据</th><th>库内当前</th><th>回传修订</th></tr></thead><tbody>${fieldRows(c,conflict)}</tbody></table></div>`
    : `<div class="basis-grid"><div><h4>原始依据</h4>${json(conflict.base)}</div><div><h4>库内当前</h4>${json(conflict.server)}</div><div><h4>回传修订</h4>${json(conflict.incoming)}</div></div>`;
  return `<div class="conflict"><p class="eyebrow">待裁决 · ${esc(kind)}</p><p>${esc(conflict.message)}</p>${sides}</div>`;
}

function resolveControls(c,item,batch,isAdmin){
  const conflict=item.conflict;
  if(!conflict||item.status!=='conflict')return '';
  const disabled=isAdmin?'':' disabled';
  const recordBtn=(choice,label)=>`<button class="merge-resolve" data-batch="${batch.id}" data-item="${item.id}" data-record="${choice}"${disabled}>${label}</button>`;
  if(conflict.kind==='delete-modify'||conflict.kind==='add-collision'||conflict.kind==='constraint'){
    const both=conflict.kind==='add-collision'?'保留双方（原键与另立底稿）':'保留双方（库内照留，回传另立底稿）';
    const labelServer=conflict.kind==='constraint'?'只用库内（放弃回传增补）':'以库内为准';
    const labelIncoming=conflict.kind==='constraint'?'只用回传（放弃库内增补）':'以回传为准';
    return `<div class="resolve-row">${recordBtn('server',labelServer)}${recordBtn('incoming',labelIncoming)}${recordBtn('both',both)}${isAdmin?'':'<small>仅负责人可裁决</small>'}</div>`;
  }
  const selects=(conflict.fields||[]).map(f=>`<label>${esc((c.fields||{})[f.field]||f.field)}<select class="merge-field" data-field="${esc(f.field)}"><option value="server">库内：${fieldValue(f.server,f.field)}</option><option value="incoming">回传：${fieldValue(f.incoming,f.field)}</option></select></label>`).join('');
  const down=conflict.downtime?`<label>停机区间<select class="merge-downtime"><option value="union">保留双方（并集）</option><option value="server">库内：${intervalText(conflict.downtime.server)}</option><option value="incoming">回传：${intervalText(conflict.downtime.incoming)}</option></select></label>`:'';
  return `<div class="resolve-grid">${selects}${down}</div><div class="resolve-row"><button class="merge-resolve-fields" data-batch="${batch.id}" data-item="${item.id}"${disabled}>按选择裁决</button>${recordBtn('both','保留双方（另立底稿）')}${isAdmin?'':'<small>仅负责人可裁决</small>'}</div>`;
}

function itemRow(c,item,batch,isAdmin){
  const diffs=item.result?.changed_fields?.length?item.result.changed_fields.map(k=>(c.fields||{})[k]||k).join('、'):'—';
  return `<article class="merge-item"><div class="merge-item-head"><h3>${esc(item.natural_key)}</h3>${badge(item.status,ITEM)}<small>${esc(item.op==='delete'?'回传撤销':'回传增改')}</small></div><p>${esc(item.message||`变更字段：${diffs}`)}</p>${basis(c,item)}${resolveControls(c,item,batch,isAdmin)}</article>`;
}

function batchCard(c,batch,session){
  const isAdmin=session?.role==='admin',canEdit=isAdmin||session?.role==='analyst';
  const s=batch.summary||{};
  const actionable=batch.status==='pending'||batch.status==='ready';
  const tools=actionable&&(canEdit||isAdmin)?`<div class="resolve-row">${canEdit?`<button class="merge-recheck" data-batch="${batch.id}">重新核验源数据</button>`:''}${isAdmin?`<button class="merge-land" data-batch="${batch.id}"${batch.status==='ready'?'':' disabled'}>整批落地</button>`:''}</div>`:'';
  return `<article class="merge-batch"><header><div><h3>批次 #${batch.id} · ${esc(batch.batch_key)}</h3><p>${esc(batch.actor||'—')} · ${time(batch.created)} · ${s.total||0} 条，自动合入 ${s.auto||0}，待裁决 ${s.conflict||0}</p></div>${badge(batch.status,STATUS)}</header>${batch.status==='landed'?`<p class="merge-landed">已于 ${time(batch.landed_at)} 整批落地；旧报告保持提交时快照不变。</p>`:''}${tools}${(batch.items||[]).map(item=>itemRow(c,item,batch,isAdmin)).join('')||'<p class="empty">展开批次查看条目。</p>'}</article>`;
}

export function merge(c,d,selected,session){
  const mergeData=d.merge||{batches:[],pending:[]};
  const isAdmin=session?.role==='admin';
  const batches=mergeData.batches||[];
  const pending=mergeData.pending||[];
  const detail=selected;
  const sample={batch_key:'offline-2026-11-17-01',note:'现场断网修订回传',items:[{natural_key:'demo-1',op:'upsert',base:'（导出包内的原始依据）',base_digest:'（导出包内的摘要）',incoming:'（离线修订后的底稿）'},{natural_key:'user-xxxx',op:'delete'}]};
  return heading('OFFLINE MERGE / 06','离线底稿合并与裁决','带出原始依据后离线增改删；独立修改自动合入，同一字段异改、撤销与修改、停机修改范围交叠均整批暂存待裁决。冲突批次不影响核算，落地整批可见，重试不重复应用。')
  +`<div class="merge-toolbar"><button id="merge-export">带出原始依据并生成回传包</button><label class="merge-file">选择回传文件<input id="merge-file" type="file" accept="application/json,.json"></label><span id="merge-file-name"></span></div>`
  +`<details open><summary>离线回传包（编辑 incoming 后回传）</summary><p class="method">每个条目含 natural_key、op（upsert/delete）；增改需带 base、base_digest 与 incoming，base 与摘要必须与导出时一致，否则拒收。点击上方按钮会用当前底稿填好回传包；把要改的 incoming 改掉、要撤销的 op 改为 delete，再回传。</p><textarea id="merge-payload" rows="14" placeholder="点击“带出原始依据并生成回传包”自动填入，或选择导出的 JSON 文件。">${esc(JSON.stringify(sample,null,2))}</textarea><div class="resolve-row"><button id="merge-submit">回传并合并</button><small>有冲突时整批暂存，不写底稿、不影响核算。</small></div></details>`
  +`<h2>待处理冲突 ${pending.length} 条</h2>${pending.length?`<ul class="pending-list">${pending.map(p=>`<li>批次 #${p.batch_id} · ${esc(p.natural_key)} · ${esc((KIND[p.conflict?.kind]||p.conflict?.kind)||'待裁决')}</li>`).join('')}</ul>`:'<p class="empty">当前没有待裁决条目；回传后若出现交叠异改或撤销与修改，会在此列出。</p>'}`
  +`<h2>合并批次</h2><div class="merge-list">${batches.map(b=>`<button class="select-merge ${detail&&detail.id===b.id?'active':''}" data-id="${b.id}">#${b.id} ${esc(b.batch_key)} ${badge(b.status,STATUS)}</button>`).join('')||'<p class="empty">还没有合并批次。先带出原始依据，离线修订后再回传。</p>'}</div>`
  +(detail?batchCard(c,detail,session):'');
}
