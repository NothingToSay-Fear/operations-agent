import {useEffect, useRef, useState} from 'react';
import {Alert, Button, Empty, Input, Modal, Progress, Select, Space, Switch, Tabs, Tag, message} from 'antd';
import {api, post} from './api';

const kinds: Record<string,string> = {work_profile:'工作背景',analysis_preference:'分析习惯',answer_preference:'回答偏好',focus_direction:'近期关注',stable_constraint:'稳定约束'};
const statuses: Record<string,string> = {queued:'等待处理',running:'处理中',completed:'已完成',failed:'失败',blocked:'等待模型',active:'生效中',disabled:'已停用',superseded:'已替代',expired:'已过期',ready:'可检索',ready_sparse:'仅词面检索',cancelled:'已取消'};
const dateText = (value?:number) => value ? new Date(value*1000).toLocaleString('zh-CN') : '长期有效';

export function SourcePreview({source,onClose}:{source:any;onClose:()=>void}) {
  const [data,setData]=useState<any>(null),[error,setError]=useState('');
  useEffect(()=>{
    setData(null);setError('');
    if(!source)return;
    const params=new URLSearchParams();
    if(source.version_id)params.set('version_id',source.version_id);
    if(source.id&&source.document_id&&source.id!==source.document_id)params.set('segment_id',source.id);
    api(`/documents/${source.document_id||source.id}/content?${params}`).then(setData).catch(e=>setError(e.message));
  },[source]);
  return <Modal title={data?.title||'资料原文'} open={!!source} onCancel={onClose} footer={null} width={850}>
    {error?<Alert type="warning" message={error}/>:<><p className="muted">版本 {data?.version||'—'} · {data?.heading||'原文'} {data?.location?.page_start?`· 第 ${data.location.page_start} 页`:''}</p><pre className="source-text">{data?.text||'正在读取…'}</pre>{data?.has_more&&<Button onClick={()=>void api<any>(`/documents/${data.id}/content?version_id=${data.version_id}&position=${data.position+12000}`).then(next=>setData({...next,text:data.text+next.text})).catch(e=>setError(e.message))}>继续读取</Button>}</>}
  </Modal>;
}

export default function ResourceCenter({open,onClose,initialTab='documents',taskId,userId,isAdmin}:{open:boolean;onClose:()=>void;initialTab?:string;taskId:string|null;userId:string;isAdmin:boolean}) {
  const [tab,setTab]=useState(initialTab),[docs,setDocs]=useState<any[]>([]),[memories,setMemories]=useState<any[]>([]),[candidates,setCandidates]=useState<any[]>([]);
  const [scope,setScope]=useState<string>(isAdmin?'shared':'private');
  const [busy,setBusy]=useState(false),[preview,setPreview]=useState<any>(null),[versions,setVersions]=useState<any[]|null>(null),[taskMemory,setTaskMemory]=useState<any>(null),[health,setHealth]=useState<any>(null);
  const [draft,setDraft]=useState<any>(null),[confirmation,setConfirmation]=useState<any>(null),[events,setEvents]=useState<any[]|null>(null);
  const fileInput=useRef<HTMLInputElement>(null),updateDoc=useRef<string|null>(null);
  const [toast,holder]=message.useMessage();
  const fail=(e:any)=>void toast.error(e.message||'操作失败');
  const refresh=async()=>{const [d,m,c]=await Promise.all([api<any[]>('/documents'),api<any[]>('/memories'),api<any[]>('/memory-candidates')]);setDocs(d);setMemories(m);setCandidates(c);};
  const run=async(work:()=>Promise<any>)=>{setBusy(true);try{await work();await refresh();}catch(e){fail(e);}finally{setBusy(false);}};
  useEffect(()=>{if(!open)return;setTab(initialTab);setScope(isAdmin?'shared':'private');void refresh().catch(fail);const timer=setInterval(()=>void refresh().catch(()=>{}),2500);return()=>clearInterval(timer);},[open,initialTab,isAdmin]);
  useEffect(()=>{if(open&&taskId)void api(`/tasks/${taskId}/memory`).then(setTaskMemory).catch(fail);else setTaskMemory(null);},[open,taskId,memories.length]);
  const upload=async(file?:File)=>{if(!file)return;await run(async()=>{const body=new FormData();body.append('file',file);if(!updateDoc.current&&!isAdmin&&scope==='shared')body.append('shared','true');await api(updateDoc.current?`/documents/${updateDoc.current}/versions`:'/documents',{method:'POST',body});void toast.success('资料已加入索引队列');});updateDoc.current=null;if(fileInput.current)fileInput.current.value='';};
  const fields=(value:any,setValue:(v:any)=>void)=><Space direction="vertical" style={{width:'100%'}}>
    <Select style={{width:'100%'}} value={value.kind} options={Object.entries(kinds).map(([value,label])=>({value,label}))} onChange={kind=>setValue({...value,kind})}/>
    <Input.TextArea rows={4} maxLength={600} value={value.content} onChange={e=>setValue({...value,content:e.target.value})} placeholder="希望 Agent 长期记住的偏好或背景"/>
    <label>有效至（留空为长期）<Input type="date" value={value.expires_at?new Date(value.expires_at*1000).toISOString().slice(0,10):''} onChange={e=>setValue({...value,expires_at:e.target.value?new Date(e.target.value+'T23:59:59').getTime()/1000:null})}/></label>
  </Space>;
  return <><Modal title="资料与记忆" open={open} onCancel={onClose} footer={null} width={1000} className="resource-center">{holder}
    <Tabs activeKey={tab} onChange={setTab} items={[
      {key:'documents',label:'运营资料',children:<>
        <p className="muted">{isAdmin?'管理员上传的资料会直接成为管理员资料；每个用户独立决定是否启用。':'个人资料默认启用；管理员资料由你独立启用。'}新版本完成前保留当前可用版本。</p>
        <Space wrap>{isAdmin?<Tag color="blue">管理员资料</Tag>:<Select value={scope} onChange={setScope} style={{minWidth:180}} options={[{value:'private',label:'我的资料'},{value:'shared',label:'管理员资料'}]}/>} 
          <Button type="primary" loading={busy} disabled={!isAdmin&&scope==='shared'} onClick={()=>{updateDoc.current=null;fileInput.current?.click();}}>{!isAdmin&&scope==='shared'?'仅管理员可上传':'上传资料'}</Button>
          <Button onClick={()=>void run(async()=>setHealth(await api('/retrieval/health')))}>检查检索模型</Button></Space>
        <input type="file" hidden ref={fileInput} accept=".txt,.md,.csv,.pdf,.docx" onChange={e=>void upload(e.target.files?.[0])}/>
        {health&&<Alert style={{marginTop:12}} type={health.error?'warning':'info'} message={`${health.mode} · 向量：${health.embedding} · 精排：${health.reranker}`} description={health.error}/>}
        <div className="resource-list">{docs.filter(d=>(d.shared?'shared':'private')===scope).map(d=><div className="resource-card" key={d.id}>
          <div className="resource-title"><strong>{d.title}</strong><Switch checked={d.enabled} aria-label={`使用${d.title}`} checkedChildren="启用" unCheckedChildren="停用" onChange={enabled=>void run(()=>api(`/documents/${d.id}`,{method:'PATCH',body:JSON.stringify({enabled})}))}/></div>
          <p><Tag>{statuses[d.status]||d.status}</Tag>当前 v{d.version} · {d.mode} · {d.characters.toLocaleString()} 字符</p>
          {['queued','running'].includes(d.status)&&<Progress percent={d.progress} size="small"/>}{d.error&&<p className="resource-warning">{d.error}</p>}
          <Space wrap><Button size="small" disabled={!d.enabled||!d.active_version_id} onClick={()=>setPreview({...d,document_id:d.id,version_id:d.active_version_id})}>原文</Button>
            <Button size="small" onClick={()=>void api<any[]>(`/documents/${d.id}/versions`).then(setVersions).catch(fail)}>版本</Button>
            {((d.owner_id===userId&&!d.shared)||(isAdmin&&d.shared))&&<><Button size="small" onClick={()=>{updateDoc.current=d.id;fileInput.current?.click();}}>更新文件</Button><Button size="small" onClick={()=>void run(()=>post(`/documents/${d.id}/retry`,{}))}>重建索引</Button>
            <Button size="small" danger onClick={()=>Modal.confirm({title:'删除资料？',content:'原文与索引将删除，已有引用后续不可读取。',onOk:()=>run(()=>api(`/documents/${d.id}`,{method:'DELETE'}))})}>删除</Button></>}
            {isAdmin&&d.shared&&<Tag color="blue">管理员资料</Tag>}</Space>
        </div>)}{!docs.some(d=>(d.shared?'shared':'private')===scope)&&<Empty description="此范围暂无资料"/>}</div>
      </>},
      {key:'memory',label:`长期记忆${candidates.length?` · ${candidates.length} 待确认`:''}`,children:<>
        <Alert type="info" message="只有确认后的记忆才会用于分析。它可以跨任务使用；其他任务的历史讨论不会被召回。"/>
        <Space style={{marginTop:14}}><Button onClick={()=>setDraft({content:'',kind:'answer_preference',expires_at:null})}>新增记忆候选</Button><Button onClick={()=>void api<any[]>('/memories/events').then(setEvents).catch(fail)}>采用与变更记录</Button></Space>
        <div className="resource-list">{candidates.map(c=><div className="resource-card" key={c.id}><Tag color="gold">等待确认</Tag><Tag>{kinds[c.kind]}</Tag><p>{c.content}</p><small className="muted">来源：{c.source} · {dateText(c.expires_at)}</small>
          {c.replaces_id&&<Alert type="warning" message="确认后将替代原记忆" description={memories.find(m=>m.id===c.replaces_id)?.content||'原记忆已不可用'}/>}
          <div style={{marginTop:10}}><Space><Button type="primary" onClick={()=>setConfirmation({...c})}>检查并确认</Button><Button onClick={()=>void run(()=>post(`/memory-candidates/${c.id}/dismiss`,{}))}>忽略</Button></Space></div></div>)}
          {memories.map(m=><div className="resource-card" key={m.id}><Tag>{kinds[m.kind]}</Tag><Tag>{statuses[m.status]||m.status}</Tag><p>{m.content}</p><small className="muted">{dateText(m.expires_at)}</small>
            <div style={{marginTop:10}}><Space><Button size="small" onClick={()=>setDraft({content:m.content,kind:m.kind,expires_at:m.expires_at,replaces_id:m.id,replaces_version:m.version})}>编辑并重新确认</Button>
              {m.status==='active'&&<Button size="small" onClick={()=>void run(()=>post(`/memories/${m.id}/control`,{version:m.version,action:'disable'}))}>停用</Button>}
              <Button size="small" danger onClick={()=>void run(()=>post(`/memories/${m.id}/control`,{version:m.version,action:'delete'}))}>忘记</Button></Space></div></div>)}
          {!memories.length&&!candidates.length&&<Empty description="尚无长期记忆"/>}</div>
      </>},
      {key:'task',label:'当前任务记忆',children:taskId&&taskMemory?<>
        <Alert type="info" message="历史检索仅限当前任务；摘要不能代替最新业务事实。"/>
        <p>摘要版本：{taskMemory.summary_version}</p><pre className="source-text">{taskMemory.summary.summary||'尚未达到压缩阈值，使用当前目标、明确条件及近期观察。'}</pre>
        <p>本任务最近采用的长期记忆</p>{taskMemory.used_memories?.map((m:any)=><div className="resource-card" key={m.id}>{memories.find(x=>x.id===m.id)?.content||'记忆已删除或失效'}<small className="muted"> · {m.reason}</small></div>)}
        <div className="resource-list">{taskMemory.jobs.map((j:any)=><div className="resource-card" key={j.id}>{j.kind==='summary'?'历史压缩':'候选提取'} · {statuses[j.status]||j.status}<p className="resource-warning">{j.error}</p></div>)}</div>
      </>:<Empty description="先打开一个任务"/>}
    ]}/>
  </Modal>
  <Modal title={draft?.replaces_id?'修改记忆候选':'新增记忆候选'} open={!!draft} onCancel={()=>setDraft(null)} okText="提交待确认" confirmLoading={busy} onOk={()=>void run(async()=>{await post('/memory-candidates',draft);setDraft(null);})}>{draft&&fields(draft,setDraft)}</Modal>
  <Modal title="确认长期记忆" open={!!confirmation} onCancel={()=>setConfirmation(null)} okText="确认并生效" confirmLoading={busy} onOk={()=>void run(async()=>{await post(`/memory-candidates/${confirmation.id}/confirm`,{version:confirmation.version,content:confirmation.content,kind:confirmation.kind,expires_at:confirmation.expires_at});setConfirmation(null);})}>
    <p>以下内容将在你确认后用于后续任务；可先修改正文、类别和有效期。</p>{confirmation&&fields(confirmation,setConfirmation)}
  </Modal>
  <Modal title="资料版本" open={!!versions} onCancel={()=>setVersions(null)} footer={null}>{versions?.map(v=><div className="resource-card" key={v.id}><strong>v{v.number} · {v.filename}</strong><p>{statuses[v.status]||v.status} · {v.mode}</p><small>{new Date(v.created_at*1000).toLocaleString('zh-CN')}</small></div>)}</Modal>
  <Modal title="记忆采用与变更记录" open={!!events} onCancel={()=>setEvents(null)} footer={null}><div className="resource-list">{events?.map((e,i)=><div className="resource-card" key={i}>{({confirmed:'用户确认',used:'任务采用',superseded:'已被替代',disable:'用户停用',delete:'用户删除'} as any)[e.action]||e.action} · {dateText(e.created_at)}<p className="muted">记忆 {e.memory_id} {e.detail?.reason||''}</p></div>)}</div></Modal>
  <SourcePreview source={preview} onClose={()=>setPreview(null)}/></>;
}
