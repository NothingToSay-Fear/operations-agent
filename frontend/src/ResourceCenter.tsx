import {useEffect, useRef, useState} from 'react';
import {Alert, Button, Empty, Input, Modal, Progress, Select, Space, Switch, Tabs, Tag, message} from 'antd';
import {api, post} from './api';
import SourcePreview from './components/SourcePreview';

export {default as SourcePreview} from './components/SourcePreview';

const memoryKinds: Record<string, string> = {
  work_profile: '工作背景',
  analysis_preference: '分析习惯',
  answer_preference: '回答偏好',
  focus_direction: '近期关注',
  stable_constraint: '稳定约束',
};

const statusLabels: Record<string, string> = {
  queued: '等待处理',
  running: '处理中',
  completed: '已完成',
  failed: '失败',
  blocked: '等待模型',
  active: '生效中',
  disabled: '已停用',
  superseded: '已替代',
  expired: '已过期',
  ready: '可检索',
  ready_sparse: '仅词面检索',
  cancelled: '已取消',
};

type ResourceCenterProps = {
  open: boolean;
  onClose: () => void;
  initialTab?: string;
  taskId: string | null;
  userId: string;
  isAdmin: boolean;
};

function formatDate(value?: number) {
  return value ? new Date(value * 1000).toLocaleString('zh-CN') : '长期有效';
}

function MemoryFields({value, onChange}: {value: any; onChange: (value: any) => void}) {
  return (
    <Space direction="vertical" style={{width: '100%'}}>
      <Select
        style={{width: '100%'}}
        value={value.kind}
        options={Object.entries(memoryKinds).map(([kind, label]) => ({value: kind, label}))}
        onChange={(kind) => onChange({...value, kind})}
      />
      <Input.TextArea
        rows={4}
        maxLength={600}
        value={value.content}
        onChange={(event) => onChange({...value, content: event.target.value})}
        placeholder="希望 Agent 长期记住的偏好或背景"
      />
      <label>
        有效至（留空为长期）
        <Input
          type="date"
          value={value.expires_at ? new Date(value.expires_at * 1000).toISOString().slice(0, 10) : ''}
          onChange={(event) => onChange({
            ...value,
            expires_at: event.target.value ? new Date(`${event.target.value}T23:59:59`).getTime() / 1000 : null,
          })}
        />
      </label>
    </Space>
  );
}

export default function ResourceCenter({
  open,
  onClose,
  initialTab = 'documents',
  taskId,
  userId,
  isAdmin,
}: ResourceCenterProps) {
  const [tab, setTab] = useState(initialTab);
  const [documents, setDocuments] = useState<any[]>([]);
  const [memories, setMemories] = useState<any[]>([]);
  const [candidates, setCandidates] = useState<any[]>([]);
  const [scope, setScope] = useState(isAdmin ? 'shared' : 'private');
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<any>(null);
  const [versions, setVersions] = useState<any[] | null>(null);
  const [taskMemory, setTaskMemory] = useState<any>(null);
  const [health, setHealth] = useState<any>(null);
  const [draft, setDraft] = useState<any>(null);
  const [confirmation, setConfirmation] = useState<any>(null);
  const [events, setEvents] = useState<any[] | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const updateDocument = useRef<string | null>(null);
  const [toast, toastHolder] = message.useMessage();

  const fail = (error: unknown) => {
    void toast.error((error as Error).message || '操作失败');
  };

  const refresh = async () => {
    const [nextDocuments, nextMemories, nextCandidates] = await Promise.all([
      api<any[]>('/documents'),
      api<any[]>('/memories'),
      api<any[]>('/memory-candidates'),
    ]);
    setDocuments(nextDocuments);
    setMemories(nextMemories);
    setCandidates(nextCandidates);
  };

  const run = async (work: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await work();
      await refresh();
    } catch (error) {
      fail(error);
    } finally {
      setBusy(false);
    }
  };

  const upload = async (file?: File) => {
    if (!file) return;
    await run(async () => {
      const body = new FormData();
      body.append('file', file);
      if (!updateDocument.current && !isAdmin && scope === 'shared') body.append('shared', 'true');
      await api(updateDocument.current ? `/documents/${updateDocument.current}/versions` : '/documents', {method: 'POST', body});
      void toast.success('资料已加入索引队列');
    });
    updateDocument.current = null;
    if (fileInput.current) fileInput.current.value = '';
  };

  useEffect(() => {
    if (!open) return;
    setTab(initialTab);
    setScope(isAdmin ? 'shared' : 'private');
    void refresh().catch(fail);
    const timer = window.setInterval(() => void refresh().catch(() => {}), 2_500);
    return () => window.clearInterval(timer);
  }, [open, initialTab, isAdmin]);

  useEffect(() => {
    if (open && taskId) {
      void api(`/tasks/${taskId}/memory`).then(setTaskMemory).catch(fail);
    } else {
      setTaskMemory(null);
    }
  }, [open, taskId, memories.length]);

  const visibleDocuments = documents.filter((document) => (document.shared ? 'shared' : 'private') === scope);
  const tabItems = [
    {
      key: 'documents',
      label: '运营资料',
      children: <DocumentTab
        documents={visibleDocuments}
        scope={scope}
        isAdmin={isAdmin}
        userId={userId}
        busy={busy}
        health={health}
        fileInput={fileInput}
        onScopeChange={setScope}
        onUpload={() => { updateDocument.current = null; fileInput.current?.click(); }}
        onFile={(file: File | undefined) => void upload(file)}
        onCheckHealth={() => void run(async () => setHealth(await api('/retrieval/health')))}
        onPreview={setPreview}
        onVersions={(id: string) => void api<any[]>(`/documents/${id}/versions`).then(setVersions).catch(fail)}
        onUpdate={(id: string) => { updateDocument.current = id; fileInput.current?.click(); }}
        onRetry={(id: string) => void run(() => post(`/documents/${id}/retry`, {}))}
        onSetEnabled={(id: string, enabled: boolean) => void run(() => api(`/documents/${id}`, {method: 'PATCH', body: JSON.stringify({enabled})}))}
        onDelete={(id: string) => Modal.confirm({
          title: '删除资料？',
          content: '原文与索引将删除，已有引用后续不可读取。',
          onOk: () => run(() => api(`/documents/${id}`, {method: 'DELETE'})),
        })}
      />,
    },
    {
      key: 'memory',
      label: `长期记忆${candidates.length ? ` · ${candidates.length} 待确认` : ''}`,
      children: <MemoryTab
        memories={memories}
        candidates={candidates}
        onNew={() => setDraft({content: '', kind: 'answer_preference', expires_at: null})}
        onEvents={() => void api<any[]>('/memories/events').then(setEvents).catch(fail)}
        onConfirm={setConfirmation}
        onDismiss={(id: string) => void run(() => post(`/memory-candidates/${id}/dismiss`, {}))}
        onEdit={(memory: any) => setDraft({
          content: memory.content,
          kind: memory.kind,
          expires_at: memory.expires_at,
          replaces_id: memory.id,
          replaces_version: memory.version,
        })}
        onControl={(id: string, body: unknown) => void run(() => post(`/memories/${id}/control`, body))}
      />,
    },
    {
      key: 'task',
      label: '当前任务记忆',
      children: <TaskMemoryTab taskId={taskId} taskMemory={taskMemory} memories={memories} />,
    },
  ];

  return (
    <>
      <Modal title="资料与记忆" open={open} onCancel={onClose} footer={null} width={1000} className="resource-center">
        {toastHolder}
        <Tabs activeKey={tab} onChange={setTab} items={tabItems} />
      </Modal>
      <MemoryDraftModal draft={draft} busy={busy} onClose={() => setDraft(null)} onChange={setDraft} onSubmit={() => void run(async () => { await post('/memory-candidates', draft); setDraft(null); })} />
      <MemoryConfirmationModal confirmation={confirmation} busy={busy} onClose={() => setConfirmation(null)} onChange={setConfirmation} onSubmit={() => void run(async () => {
        await post(`/memory-candidates/${confirmation.id}/confirm`, {
          version: confirmation.version,
          content: confirmation.content,
          kind: confirmation.kind,
          expires_at: confirmation.expires_at,
        });
        setConfirmation(null);
      })} />
      <VersionModal versions={versions} onClose={() => setVersions(null)} />
      <EventModal events={events} onClose={() => setEvents(null)} />
      <SourcePreview source={preview} onClose={() => setPreview(null)} />
    </>
  );
}

function DocumentTab(props: any) {
  const {
    documents, scope, isAdmin, userId, busy, health, fileInput,
    onScopeChange, onUpload, onFile, onCheckHealth, onPreview, onVersions, onUpdate, onRetry, onSetEnabled, onDelete,
  } = props;
  return (
    <>
      <p className="muted">{isAdmin ? '管理员上传的资料会直接成为管理员资料；每个用户独立决定是否启用。' : '个人资料默认启用；管理员资料由你独立启用。'}新版本完成前保留当前可用版本。</p>
      <Space wrap>
        {isAdmin ? <Tag color="blue">管理员资料</Tag> : <Select value={scope} onChange={onScopeChange} style={{minWidth: 180}} options={[{value: 'private', label: '我的资料'}, {value: 'shared', label: '管理员资料'}]} />}
        <Button type="primary" loading={busy} disabled={!isAdmin && scope === 'shared'} onClick={onUpload}>{!isAdmin && scope === 'shared' ? '仅管理员可上传' : '上传资料'}</Button>
        <Button onClick={onCheckHealth}>检查检索模型</Button>
      </Space>
      <input type="file" hidden ref={fileInput} accept=".txt,.md,.csv,.pdf,.docx" onChange={(event) => onFile(event.target.files?.[0])} />
      {health && <Alert style={{marginTop: 12}} type={health.error ? 'warning' : 'info'} message={`${health.mode} · 向量：${health.embedding} · 精排：${health.reranker}`} description={health.error} />}
      <div className="resource-list">
        {documents.map((document: any) => (
          <div className="resource-card" key={document.id}>
            <div className="resource-title">
              <strong>{document.title}</strong>
              <Switch checked={document.enabled} aria-label={`使用${document.title}`} checkedChildren="启用" unCheckedChildren="停用" onChange={(enabled) => onSetEnabled(document.id, enabled)} />
            </div>
            <p><Tag>{statusLabels[document.status] || document.status}</Tag>当前 v{document.version} · {document.mode} · {document.characters.toLocaleString()} 字符</p>
            {['queued', 'running'].includes(document.status) && <Progress percent={document.progress} size="small" />}
            {document.error && <p className="resource-warning">{document.error}</p>}
            <Space wrap>
              <Button size="small" disabled={!document.enabled || !document.active_version_id} onClick={() => onPreview({...document, document_id: document.id, version_id: document.active_version_id})}>原文</Button>
              <Button size="small" onClick={() => onVersions(document.id)}>版本</Button>
              {((document.owner_id === userId && !document.shared) || (isAdmin && document.shared)) && <>
                <Button size="small" onClick={() => onUpdate(document.id)}>更新文件</Button>
                <Button size="small" onClick={() => onRetry(document.id)}>重建索引</Button>
                <Button size="small" danger onClick={() => onDelete(document.id)}>删除</Button>
              </>}
              {isAdmin && document.shared && <Tag color="blue">管理员资料</Tag>}
            </Space>
          </div>
        ))}
        {!documents.length && <Empty description="此范围暂无资料" />}
      </div>
    </>
  );
}

function MemoryTab({memories, candidates, onNew, onEvents, onConfirm, onDismiss, onEdit, onControl}: any) {
  return (
    <>
      <Alert type="info" message="只有确认后的记忆才会用于分析。它可以跨任务使用；其他任务的历史讨论不会被召回。" />
      <Space style={{marginTop: 14}}><Button onClick={onNew}>新增记忆候选</Button><Button onClick={onEvents}>采用与变更记录</Button></Space>
      <div className="resource-list">
        {candidates.map((candidate: any) => <div className="resource-card" key={candidate.id}>
          <Tag color="gold">等待确认</Tag><Tag>{memoryKinds[candidate.kind]}</Tag><p>{candidate.content}</p><small className="muted">来源：{candidate.source} · {formatDate(candidate.expires_at)}</small>
          {candidate.replaces_id && <Alert type="warning" message="确认后将替代原记忆" description={memories.find((memory: any) => memory.id === candidate.replaces_id)?.content || '原记忆已不可用'} />}
          <div style={{marginTop: 10}}><Space><Button type="primary" onClick={() => onConfirm({...candidate})}>检查并确认</Button><Button onClick={() => onDismiss(candidate.id)}>忽略</Button></Space></div>
        </div>)}
        {memories.map((memory: any) => <div className="resource-card" key={memory.id}>
          <Tag>{memoryKinds[memory.kind]}</Tag><Tag>{statusLabels[memory.status] || memory.status}</Tag><p>{memory.content}</p><small className="muted">{formatDate(memory.expires_at)}</small>
          <div style={{marginTop: 10}}><Space><Button size="small" onClick={() => onEdit(memory)}>编辑并重新确认</Button>{memory.status === 'active' && <Button size="small" onClick={() => onControl(memory.id, {version: memory.version, action: 'disable'})}>停用</Button>}<Button size="small" danger onClick={() => onControl(memory.id, {version: memory.version, action: 'delete'})}>忘记</Button></Space></div>
        </div>)}
        {!memories.length && !candidates.length && <Empty description="尚无长期记忆" />}
      </div>
    </>
  );
}

function TaskMemoryTab({taskId, taskMemory, memories}: {taskId: string | null; taskMemory: any; memories: any[]}) {
  if (!taskId || !taskMemory) return <Empty description="先打开一个任务" />;
  return <>
    <Alert type="info" message="历史检索仅限当前任务；摘要不能替代最新业务事实。" />
    <p>当前有效约束 · v{taskMemory.constraint_state_version}</p><pre className="source-text">{JSON.stringify(taskMemory.effective_constraints, null, 2)}</pre>
    <p>近期原文窗口 · {taskMemory.recent_turns?.length || 0} 条</p><div className="resource-list">{taskMemory.recent_turns?.map((turn: any) => <div className="resource-card" key={turn.message_id}><Tag>{turn.role === 'user' ? '用户' : 'Agent'}</Tag>{turn.content}</div>)}</div>
    <p>摘要版本：{taskMemory.summary_version}</p><pre className="source-text">{taskMemory.summary.summary || '尚未达到压缩阈值，使用当前目标、明确条件及近期观察。'}</pre>
    <p>本任务最近采用的长期记忆</p>{taskMemory.used_memories?.map((item: any) => <div className="resource-card" key={item.id}>{memories.find((memory) => memory.id === item.id)?.content || '记忆已删除或失效'}<small className="muted"> · {item.reason}</small></div>)}
    <div className="resource-list">{taskMemory.jobs.map((job: any) => <div className="resource-card" key={job.id}>{job.kind === 'summary' ? '历史压缩' : '候选提取'} · {statusLabels[job.status] || job.status}<p className="resource-warning">{job.error}</p></div>)}</div>
  </>;
}

function MemoryDraftModal({draft, busy, onClose, onChange, onSubmit}: any) {
  return <Modal title={draft?.replaces_id ? '修改记忆候选' : '新增记忆候选'} open={Boolean(draft)} onCancel={onClose} okText="提交待确认" confirmLoading={busy} onOk={onSubmit}>{draft && <MemoryFields value={draft} onChange={onChange} />}</Modal>;
}

function MemoryConfirmationModal({confirmation, busy, onClose, onChange, onSubmit}: any) {
  return <Modal title="确认长期记忆" open={Boolean(confirmation)} onCancel={onClose} okText="确认并生效" confirmLoading={busy} onOk={onSubmit}><p>以下内容将在你确认后用于后续任务；可先修改正文、类别和有效期。</p>{confirmation && <MemoryFields value={confirmation} onChange={onChange} />}</Modal>;
}

function VersionModal({versions, onClose}: {versions: any[] | null; onClose: () => void}) {
  return <Modal title="资料版本" open={Boolean(versions)} onCancel={onClose} footer={null}>{versions?.map((version) => <div className="resource-card" key={version.id}><strong>v{version.number} · {version.filename}</strong><p>{statusLabels[version.status] || version.status} · {version.mode}</p><small>{new Date(version.created_at * 1000).toLocaleString('zh-CN')}</small></div>)}</Modal>;
}

function EventModal({events, onClose}: {events: any[] | null; onClose: () => void}) {
  const labels: Record<string, string> = {confirmed: '用户确认', used: '任务采用', superseded: '已被替代', disable: '用户停用', delete: '用户删除'};
  return <Modal title="记忆采用与变更记录" open={Boolean(events)} onCancel={onClose} footer={null}><div className="resource-list">{events?.map((event, index) => <div className="resource-card" key={index}>{labels[event.action] || event.action} · {formatDate(event.created_at)}<p className="muted">记忆 {event.memory_id} {event.detail?.reason || ''}</p></div>)}</div></Modal>;
}
