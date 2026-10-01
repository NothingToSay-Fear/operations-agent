import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Drawer, Empty, Input, Modal, Spin, Tabs, Tag, Tooltip, message } from 'antd';
import { ArrowUpOutlined, ArrowRightOutlined, PlusOutlined, FileTextOutlined, BarChartOutlined,
  CheckOutlined, PauseOutlined, PlayCircleOutlined, StopOutlined, LogoutOutlined,
  BookOutlined, DownloadOutlined, EditOutlined, BulbOutlined, LoadingOutlined, MenuOutlined,
  DeleteOutlined } from '@ant-design/icons';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, post, Task, Artifact, ConversationTurn } from './api';
import ResourceCenter, {SourcePreview} from './ResourceCenter';

const STATUS: Record<string, string> = {queued: '待执行', running: '执行中', waiting_user: '等待补充', paused: '已暂停',
  completed: '已完成', partial: '部分完成', blocked: '待处理', failed: '执行失败', cancelled: '已取消'};
const TOOL: Record<string, string> = {inspect_data_capabilities: '了解数据范围', get_metric_definitions: '核对指标口径', query_metrics: '查询经营指标',
  compare_metrics: '对比周期与贡献', get_products: '读取商品信息', query_order_facts: '调查订单与退款', query_inventory: '检查库存与在途', query_marketing: '分析渠道与活动',
  search_knowledge: '检索运营资料', read_document: '阅读资料原文', calculate: '验证计算', save_artifact: '保存工作成果', read_evidence: '复核已有证据'};
const SUGGESTIONS = [
  {tag: '经营诊断', title: '销售变化，找到关键贡献项', icon: <BarChartOutlined />, goal: '分析数据截止日期之前七天的GMV，相比前七天下降或增长的主要贡献项是什么？结合渠道和商品数据，给出三个有证据的运营建议。'},
  {tag: '库存决策', title: '提前发现补货与缺货风险', icon: <span>◫</span>, goal: '根据最新库存、在途和销售情况，找出未来两周有缺货风险的SKU，给出补货优先级和数量建议。明确交期、最小订货量和需求假设。'},
  {tag: '活动方案', title: '让活动兼顾销量与毛利', icon: <BulbOutlined />, goal: '结合现有商品成本、库存与活动规则，制定未来一周的促销建议，说明参与商品、折扣、毛利约束和执行前需要验证的信息，保存一份方案。'},
  {tag: '商品优化', title: '用准确的卖点改善商品表达', icon: <EditOutlined />, goal: '读取前五个SKU的商品信息和品牌文案规范，优化商品标题与卖点，保留可验证的属性，不虚构材质或功效，保存文案草稿。'},
];
const number = (n: number | null | undefined, digits = 0) => n == null ? '—' : n.toLocaleString('zh-CN', {maximumFractionDigits: digits});

function Brand() { return <div className="brand"><div className="brand-symbol">序</div><span>序策<small>OPERATIONS AGENT</small></span></div>; }

function Markdown({text, onEvidence, onArtifact}: {text: string; onEvidence?: (id: string) => void; onArtifact?: (id: string) => void}) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}
    urlTransform={url => /^(evidence:ev_|artifact:ar_)[a-zA-Z0-9_-]+$/.test(url) ? url : defaultUrlTransform(url)}
    components={{a: ({href, children}) => href?.startsWith('evidence:') ? <button className="inline-link" onClick={() => onEvidence?.(href.slice(9))}>{children}</button>
      : href?.startsWith('artifact:') ? <button className="inline-link" onClick={() => onArtifact?.(href.slice(9))}>{children}</button>
      : <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>}}>{text}</ReactMarkdown></div>;
}

function ConversationMessage({turn,onEvidence,onArtifact}:{turn:ConversationTurn;onEvidence:(id:string)=>void;onArtifact:(id:string)=>void}) {
  if(turn.role==='user')return <div className="user-message"><span className="message-caption">{turn.kind==='goal'?'任务目标':'追问'}</span><p>{turn.content}</p></div>;
  const title=turn.kind==='question'?'需要补充':turn.kind==='notice'?'当前进展':'分析成果';
  return <div className="agent-message answer"><div className="agent-heading"><div className="mini-brand">序</div><strong>{title}</strong></div><Markdown text={turn.content} onEvidence={onEvidence} onArtifact={onArtifact}/><div className="answer-usage">本轮模型调用 {turn.model_calls ?? 0} 次</div></div>;
}

function Login({onLogin}: {onLogin: (user: any) => void}) {
  const [register, setRegister] = useState(false), [username, setUsername] = useState(''), [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const submit = async () => {
    setBusy(true); setError('');
    try { onLogin(await post(`/auth/${register ? 'register' : 'login'}`, {username, password})); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return <main className="login-page"><section className="login-story"><Brand /><div><div className="eyebrow">从经营问题，到有依据的行动</div>
    <h1>把目标交给 Agent。<br/><em>让证据指引下一步。</em></h1><p>理解目标，自主规划，持续调查。<br/>在商品、订单、库存与活动之间，找到值得采取的行动。</p></div>
    <div className="story-footer"><span className="status-dot" /> 模拟经营数据 · 只读分析与建议</div></section>
    <section className="login-form"><div className="eyebrow">你的运营工作空间</div><h2>{register ? '创建账号' : '欢迎回来'}</h2><p>继续你的分析与决策。</p>
      <form onSubmit={e => { e.preventDefault(); void submit(); }}>
      <label>用户名<Input size="large" value={username} onChange={e => setUsername(e.target.value)} placeholder="至少 3 个字符" autoComplete="username" /></label>
      <label>密码<Input.Password size="large" value={password} onChange={e => setPassword(e.target.value)} placeholder="至少 8 个字符" autoComplete={register ? 'new-password' : 'current-password'} /></label>
      {error && <Alert type="error" message={error} showIcon />}
      <Button htmlType="submit" type="primary" size="large" block loading={busy}>{register ? '创建并进入' : '进入工作台'} <ArrowRightOutlined /></Button></form>
      <button className="text-button" onClick={() => { setRegister(!register); setError(''); }}>{register ? '已有账号，返回登录' : '首次使用？创建一个账号'}</button>
    </section></main>;
}

function Trend({rows}: {rows: any[]}) {
  if (rows.length < 2) return <div className="chart-empty">暂无趋势数据</div>;
  const max = Math.max(...rows.map(r => r.paid_gmv), 1), width = 640, height = 130;
  const points = rows.map((r, i) => `${i/(rows.length-1)*width},${height-r.paid_gmv/max*(height-15)}`).join(' ');
  return <div className="trend"><svg viewBox={`0 0 ${width} ${height+6}`} preserveAspectRatio="none" role="img" aria-label="近十四天支付商品GMV趋势">
    <defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#b5e899" stopOpacity=".2"/><stop offset="100%" stopColor="#b5e899" stopOpacity="0"/></linearGradient></defs>
    {[25,70,115].map(y => <line key={y} x1="0" x2={width} y1={y} y2={y} stroke="#ffffff0a"/>)}
    <polygon points={`0,${height} ${points} ${width},${height}`} fill="url(#fill)"/>
    <polyline points={points} fill="none" stroke="#b5e899" strokeWidth="2.5" strokeLinejoin="round"/>
  </svg><div className="chart-axis"><span>{rows[0].group}</span><span>按支付日 · 不含运费</span><span>{rows[rows.length-1].group}</span></div></div>;
}

export default function App() {
  const [user, setUser] = useState<any>(null), [checking, setChecking] = useState(true);
  const [tasks, setTasks] = useState<Task[]>([]), [selected, setSelected] = useState<string | null>(null), [task, setTask] = useState<Task | null>(null);
  const [conversation,setConversation]=useState<ConversationTurn[]>([]);
  const [overview, setOverview] = useState<any>(null), [dataError, setDataError] = useState(''), [config, setConfig] = useState<any>(null);
  const [input, setInput] = useState(''), [busy, setBusy] = useState(false), [docsOpen, setDocsOpen] = useState(false);
  const [pendingMemories,setPendingMemories]=useState(0);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]), [artifact, setArtifact] = useState<Artifact | null>(null), [editing, setEditing] = useState(false), [editText, setEditText] = useState('');
  const [evidence, setEvidence] = useState<any>(null), [audit, setAudit] = useState<any>(null), [eventText, setEventText] = useState('');
  const [toast, holder] = message.useMessage();
  const [mobileMenu, setMobileMenu] = useState(false);
  const selectedRef = useRef(selected); selectedRef.current = selected;
  useEffect(()=>{
    if(!selected||!user){setPendingMemories(0);return;}
    const refresh=()=>api<any[]>('/memory-candidates').then(rows=>setPendingMemories(rows.filter(r=>r.task_id===selected).length)).catch(()=>{});
    void refresh();const timer=setInterval(()=>void refresh(),4000);return()=>clearInterval(timer);
  },[selected,user]);
  const [centerTab,setCenterTab]=useState('documents');
  const [sourcePreview,setSourcePreview]=useState<any>(null);
  const showError = (error: unknown) => { void toast.error((error as Error).message); };
  const loadTasks = async () => setTasks(await api('/tasks'));
  const refreshTask = async (id: string) => {
    const [next, files, turns] = await Promise.all([api<Task>(`/tasks/${id}`), api<Artifact[]>(`/tasks/${id}/artifacts`), api<ConversationTurn[]>(`/tasks/${id}/conversation`)]);
    if (selectedRef.current === id) { setTask(next); setArtifacts(files); setConversation(turns); }
  };
  useEffect(() => { api('/auth/me').then(setUser).catch(() => {}).finally(() => setChecking(false)); }, []);
  useEffect(() => {
    if (!user) return;
    void loadTasks().catch(showError);
    api('/overview').then(data => {setOverview(data); setDataError('');}).catch(e => setDataError(e.message));
    api('/config').then(setConfig).catch(showError);
  }, [user]);
  useEffect(() => {
    setTask(null); setArtifacts([]); setConversation([]); setEventText('');
    if (!selected) return;
    void refreshTask(selected).catch(showError);
  }, [selected]);
  useEffect(() => {
    if (!selected || !task || !['queued','running'].includes(task.status)) return;
    const id = selected;
    const source = new EventSource(`/api/tasks/${id}/events?after=${task.state?.seq ?? 0}`, {withCredentials: true});
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => { if (timer) clearTimeout(timer); timer = setTimeout(() => {void refreshTask(id).catch(showError); void loadTasks().catch(showError);}, 150); };
    source.addEventListener('update', event => {
      const data = JSON.parse((event as MessageEvent).data);
      const p = data.payload;
      const toolText = Array.isArray(p.tool)
        ? `正在并行执行：${p.tool.map((name: string) => TOOL[name] || name).join('、')}`
        : TOOL[p.tool];
      setEventText(p.summary || p.reason || p.message || toolText || (data.kind === 'model_started' ? '正在分析当前目标与证据…' : '正在推进任务…'));
      refresh();
    });
    source.addEventListener('done', () => { source.close(); refresh(); });
    source.onerror = () => { void refreshTask(id).catch(showError); };
    return () => { source.close(); if (timer) clearTimeout(timer); };
  }, [selected, task?.status]);
  const submit = async (goal = input) => {
    if (!goal.trim()) return;
    setBusy(true);
    try {
      if (selected) {await post(`/tasks/${selected}/control`, {action:'message', message:goal}); await refreshTask(selected);}
      else {const created = await post<Task>('/tasks', {goal}); setSelected(created.id);}
      setInput(''); await loadTasks();
    } catch (e) {showError(e);} finally {setBusy(false);}
  };
  const control = async (action: string) => {
    if (!selected) return;
    try {await post(`/tasks/${selected}/control`, {action}); await refreshTask(selected); await loadTasks();} catch (e) {showError(e);}
  };
  const confirmDeleteTask = (id: string) => {
    Modal.confirm({
      title: '删除当前会话？',
      content: '消息、计划、运行记录、证据、成果、任务记忆、后台作业，以及由本会话确认生成的长期记忆都将永久删除。',
      okText: '永久删除',
      cancelText: '取消',
      okButtonProps: {danger: true},
      onOk: async () => {
        try {
          await api(`/tasks/${id}`, {method: 'DELETE'});
          setTasks(rows => rows.filter(row => row.id !== id));
          if (selectedRef.current === id) {
            setSelected(null); setTask(null); setArtifacts([]); setConversation([]); setArtifact(null);
            setEvidence(null); setAudit(null); setPendingMemories(0); setEventText('');
            setDocsOpen(false);
          }
          void toast.success('会话及相关内容已删除');
        } catch (error) {
          showError(error);
          throw error;
        }
      },
    });
  };
  const openEvidence = async (id: string) => {try {setEvidence(await api(`/tasks/${selected}/evidence/${id}`));} catch (e) {showError(e);}};
  const openArtifact = (id: string) => {const file = artifacts.find(a => a.id===id); if (file) {setArtifact(file); setEditing(false);} else void toast.info('成果版本尚未加载，请稍后重试');};
  if (checking) return <div className="full-loading"><Spin /></div>;
  if (!user) return <>{holder}<Login onLogin={setUser}/></>;
  const current = overview?.current.rows[0], previous = overview?.previous.rows[0];
  const active = task && ['running','queued'].includes(task.status);
  const latestAssistant=conversation.length&&conversation[conversation.length-1].role==='assistant'?conversation[conversation.length-1]:null;
  const earlierConversation=latestAssistant?conversation.slice(0,-1):conversation;
  const metrics = [
    {label:'支付商品 GMV', key:'paid_gmv', prefix:'¥', digits:0}, {label:'支付订单', key:'paid_orders', prefix:'', digits:0},
    {label:'商品客单价', key:'aov', prefix:'¥', digits:2}, {label:'退款到账额', key:'refund_amount', prefix:'¥', digits:0},
  ];
  return <div className="app-shell">{holder}
    <aside className="sidebar"><Brand/><Button className="new-task" type="primary" icon={<PlusOutlined/>} onClick={() => {setSelected(null); setInput('');}}>发起新任务</Button>
      <button className={`nav-item ${!selected?'active':''}`} onClick={() => setSelected(null)}><BarChartOutlined/> 运营总览</button>
      <button className="nav-item" onClick={() => {setCenterTab('documents');setDocsOpen(true);}}><BookOutlined/> 运营资料 <span className="nav-arrow">↗</span></button>
      <button className="nav-item" onClick={()=>{setCenterTab('memory');setDocsOpen(true);}}><BulbOutlined/> 长期记忆</button>
      <div className="sidebar-label">最近任务 <span>{tasks.length}</span></div>
      <div className="task-list">{tasks.length ? tasks.map(t => <div key={t.id} className={`task-row ${selected===t.id?'selected':''}`}>
        <button className="task-link" onClick={() => setSelected(t.id)}><span className={`task-indicator ${t.status}`}/><span>{t.goal}</span></button>
        <Tooltip title="删除会话"><Button className="task-delete" type="text" danger size="small" icon={<DeleteOutlined/>} aria-label={`删除会话：${t.goal}`} onClick={()=>confirmDeleteTask(t.id)}/></Tooltip>
      </div>) : <p className="muted small">任务会保存在这里</p>}</div>
      <div className="sidebar-bottom"><div className="environment"><span className="status-dot"/><span>模拟经营环境<small>只读数据 · 自主分析</small></span></div>
      <div className="profile"><div className="avatar">{user.username.slice(0,1).toUpperCase()}</div><span>{user.username}</span><Tooltip title="退出登录"><Button type="text" icon={<LogoutOutlined/>} aria-label="退出登录" onClick={async () => {await post('/auth/logout', {}); setUser(null); setSelected(null); setTasks([]);}}/></Tooltip></div></div>
    </aside>
    <main className="workspace"><header className="topbar"><Button className="mobile-nav-trigger" type="text" icon={<MenuOutlined/>} aria-label="打开导航" onClick={()=>setMobileMenu(true)}/><span>工作空间 <span className="slash">/</span> {selected?'分析任务':'运营总览'}</span>
      <div><Tag className="simulation-tag">模拟数据</Tag><span className="readonly">◈ 业务只读</span></div></header>
      {!selected ? <div className="overview page-enter"><div className="heading-row"><div><div className="eyebrow">YOUR OPERATIONS, IN FOCUS</div><h1>看清经营，决定下一步<span className="lime">。</span></h1><p>从一个问题开始，让 Agent 规划、调查并交付有依据的建议。</p></div>
        <div className="date-block"><span>数据截止</span><strong>{overview?.dataset.as_of || '—'}</strong><small>Asia / Shanghai</small></div></div>
        {dataError && <Alert type="warning" message={dataError} showIcon className="notice"/>}
        {config && !config.model_ready && <Alert type="info" message="模型尚未连接。经营数据可以查看，分析任务会保留并等待模型配置。" showIcon className="notice"/>}
        <div className="metric-grid">{metrics.map(m => {
          const before = previous?.[m.key], now = current?.[m.key];
          const delta = before ? (now-before)/before*100 : null;
          return <div className="metric-card" key={m.key}><div className="metric-label">{m.label}<span>近 7 天</span></div><strong><small>{m.prefix}</small>{number(now,m.digits)}</strong><div className="metric-comparison"><span className={delta!==null&&delta>=0?'positive':'neutral'}>{delta===null?'—':`${delta>=0?'↗':'↘'} ${Math.abs(delta).toFixed(1)}%`}</span><span>较前 7 天</span></div></div>;
        })}</div>
        <section className="chart-panel"><div className="panel-title"><span><span className="status-dot"/> 销售趋势</span><span className="muted small">近 14 天 / 支付商品 GMV</span></div><Trend rows={overview?.trend.rows || []}/></section>
        <div className="section-heading"><h2>今天想解决什么问题？</h2><span>给出目标，Agent 自主推进</span></div>
        <div className="suggestion-grid">{SUGGESTIONS.map(s => <button className="suggestion" key={s.tag} disabled={busy} onClick={() => setInput(s.goal)}><div className="suggestion-top"><span>{s.icon}</span><ArrowRightOutlined/></div><small>{s.tag}</small><strong>{s.title}</strong></button>)}</div>
        <div className="composer home-composer"><Input.TextArea autoSize={{minRows:2,maxRows:6}} value={input} onChange={e=>setInput(e.target.value)} placeholder="描述你的目标，例如：最近销售波动的主要贡献项是什么？" onKeyDown={e => {if(e.key==='Enter' && !e.shiftKey && !e.nativeEvent.isComposing){e.preventDefault();void submit();}}}/>
          <div className="composer-footer"><span><span className="tiny-orbit"/> 自主规划 · 持续验证 · 证据可追溯</span><Button type="primary" shape="circle" icon={<ArrowUpOutlined/>} aria-label="开始分析" loading={busy} disabled={!input.trim()} onClick={()=>void submit()}/></div></div>
        <p className="footnote">模拟数据用于分析验证。所有输出为建议，不会修改商品、库存或投放。</p>
      </div> : <div className="task-layout"><section className="conversation">
        {!task ? <div className="full-loading"><Spin/></div> : <>
          <div className="task-heading"><div className="eyebrow">AGENT WORKSPACE</div><div className="task-title-row"><h2>分析与执行</h2><Tag color={task.status==='completed'?'green':active?'processing':'default'}>{STATUS[task.status]}</Tag></div></div>
          <div className="messages">{earlierConversation.map(turn=><ConversationMessage key={turn.id} turn={turn} onEvidence={openEvidence} onArtifact={openArtifact}/>)}
            {task.state.plan && <div className="agent-message"><div className="agent-heading"><div className="mini-brand">序</div><strong>当前计划</strong><span>v{task.state.plans.length}</span></div><p>{task.state.plan.summary}</p>
              {task.state.plans.length>1 && <div className="replan-note">计划更新 · {task.state.plan.change_reason}</div>}</div>}
            {active && <div className="live-status"><LoadingOutlined spin/><span>{eventText || '正在理解目标并准备下一步…'}</span></div>}
            {task.state.waiting_question && task.status==='waiting_user' && latestAssistant?.kind!=='question' && <Alert type="info" showIcon message="需要你补充一项信息" description={task.state.waiting_question}/>}
            {pendingMemories>0&&<Alert type="info" message={`有 ${pendingMemories} 条长期记忆等待确认，尚未生效`} action={<Button onClick={()=>{setCenterTab('memory');setDocsOpen(true);}}>检查候选</Button>}/>}
            {latestAssistant&&<ConversationMessage turn={latestAssistant} onEvidence={openEvidence} onArtifact={openArtifact}/>}
            {artifacts.length>0 && <div className="artifact-list">{artifacts.map(a=><button key={a.id} className="artifact-card" onClick={()=>openArtifact(a.id)}><FileTextOutlined/><span><strong>{a.title}</strong><small>{a.format.toUpperCase()} · 版本 {a.version}</small></span><ArrowRightOutlined/></button>)}</div>}
          </div>
          <div className="task-composer-area"><div className="task-controls">{active ? <Button size="small" icon={<PauseOutlined/>} onClick={()=>void control('pause')}>暂停</Button>
            : ['paused','blocked','failed','partial'].includes(task.status) ? <Button size="small" icon={<PlayCircleOutlined/>} onClick={()=>void control('resume')}>继续任务</Button> : null}
            {!['completed','cancelled'].includes(task.status) && <Button size="small" type="text" icon={<StopOutlined/>} onClick={()=>void control('cancel')}>取消任务</Button>}
            <button className="text-button audit-link" onClick={async()=>{try {setAudit(await api(`/tasks/${selected}/audit`));}catch(e){showError(e);}}}>查看运行记录</button></div>
            <Button size="small" type="text" onClick={()=>{setCenterTab('task');setDocsOpen(true);}}>查看任务记忆与采用偏好</Button>
            <div className="composer"><Input.TextArea value={input} autoSize={{minRows:2,maxRows:5}} disabled={task.status==='cancelled'} onChange={e=>setInput(e.target.value)} placeholder="补充条件、调整目标，或回答 Agent 的问题…" onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void submit();}}}/>
            <div className="composer-footer"><span>修改条件后，将重新评估计划与证据</span><Button type="primary" shape="circle" icon={<ArrowUpOutlined/>} aria-label="发送补充条件" loading={busy} disabled={!input.trim()||task.status==='cancelled'} onClick={()=>void submit()}/></div></div></div>
        </>}
      </section><aside className="execution-panel"><Tabs items={[
        {key:'plan',label:'执行计划',children:task?.state.plan?<><div className="plan-caption">根据观察持续调整 <Tag>v{task.state.plans.length}</Tag></div><ol className="plan-list">{task.state.plan.steps.map((s,i)=>{
          const done=task.state.steps[s.id]?.status==='succeeded', skipped=task.state.steps[s.id]?.status==='skipped';
          return <li key={s.id} className={done?'done':''}><div className="step-number">{done?<CheckOutlined/>:skipped?'—':i+1}</div><div><strong>{s.objective}</strong><p>{s.done_when}</p>{(done||skipped)&&<span className="step-complete">{done?'已完成':'目标已满足，无需继续'}</span>}</div></li>;
        })}</ol><div className="success-criteria"><h4>完成标准</h4>{task.state.plan.criteria.map(c=><p key={c.id}>◇ {c.description}</p>)}</div></>:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="计划将在这里展示"/>},
        {key:'evidence',label:`证据 ${task?.state.observations.length||0}`,children:<div className="evidence-list">{task?.state.observations.map((o,i)=><button key={o.evidence_id} onClick={()=>void openEvidence(o.evidence_id)}><span className={`evidence-status ${o.status}`}/><div><strong>{TOOL[o.tool]||o.tool}</strong><small>观察 {i+1} · {o.status==='success'?'已获取':o.status==='empty'?'无数据':'失败'}</small></div><ArrowRightOutlined/></button>)}</div>}
      ]}/><div className="execution-footer"><span className="status-dot"/> 所有经营数据保持只读</div></aside></div>}
    </main>
    <Drawer title="工作空间" className="mobile-drawer" placement="left" width={300} open={mobileMenu} onClose={()=>setMobileMenu(false)}>
      <button className="nav-item" onClick={()=>{setSelected(null);setMobileMenu(false);}}><BarChartOutlined/>运营总览</button>
      <button className="nav-item" onClick={()=>{setCenterTab('documents');setDocsOpen(true);setMobileMenu(false);}}><BookOutlined/>运营资料</button>
      <button className="nav-item" onClick={()=>{setCenterTab('memory');setDocsOpen(true);setMobileMenu(false);}}><BulbOutlined/>长期记忆</button>
      <p className="muted small">最近任务</p><div className="task-list">{tasks.map(t=><div key={t.id} className={`task-row ${selected===t.id?'selected':''}`}>
        <button className="task-link" onClick={()=>{setSelected(t.id);setMobileMenu(false);}}><span className={`task-indicator ${t.status}`}/><span>{t.goal}</span></button>
        <Tooltip title="删除会话"><Button className="task-delete" type="text" danger size="small" icon={<DeleteOutlined/>} aria-label={`删除会话：${t.goal}`} onClick={()=>confirmDeleteTask(t.id)}/></Tooltip>
      </div>)}</div>
      <Button type="text" icon={<LogoutOutlined/>} onClick={async()=>{await post('/auth/logout',{});setUser(null);setSelected(null);setTasks([]);setMobileMenu(false);}}>退出登录</Button>
    </Drawer>
    <ResourceCenter open={docsOpen} onClose={()=>setDocsOpen(false)} initialTab={centerTab} taskId={selected} userId={user.id} isAdmin={!!user.is_admin}/>
    <SourcePreview source={sourcePreview} onClose={()=>setSourcePreview(null)}/>
    <Modal title={artifact?`${artifact.title} · v${artifact.version}`:''} open={!!artifact} onCancel={()=>setArtifact(null)} width={900} footer={artifact?<>
      <Button icon={<DownloadOutlined/>} href={`/api/tasks/${selected}/artifacts/${artifact.id}/download`}>下载</Button>
      <Button icon={<EditOutlined/>} onClick={()=>{setEditing(!editing);setEditText(artifact.content);}}>{editing?'取消编辑':'编辑副本'}</Button>
      {editing&&<Button type="primary" onClick={async()=>{try{await api(`/tasks/${selected}/artifacts/${artifact.id}`,{method:'PATCH',body:JSON.stringify({content:editText})});await refreshTask(selected!);setArtifact(null);void toast.success('已保存新版本');}catch(e){showError(e);}}}>保存新版本</Button>}</>:null}>
      {artifact&&(editing?<Input.TextArea rows={20} value={editText} onChange={e=>setEditText(e.target.value)}/>:artifact.format==='csv'?<pre className="json-view">{artifact.content}</pre>:<Markdown text={artifact.content} onEvidence={openEvidence} onArtifact={openArtifact}/>)}
    </Modal>
    <Modal title="证据详情" open={!!evidence} onCancel={()=>setEvidence(null)} footer={null} width={860}>
      {evidence?.result?.data?.rows?.filter((r:any)=>r.document_id).map((r:any)=><Button key={r.id} onClick={()=>setSourcePreview(r)}>{r.title} · {r.heading||'查看原文'}</Button>)}
      {evidence?.result?.data?.rows?.filter((r:any)=>r.task_id).map((r:any)=><Button key={r.id} onClick={()=>void api(`/tasks/${selected}/history/${r.id}`).then(setEvidence).catch(showError)}>当前任务历史 · {r.seq}</Button>)}
      <pre className="json-view">{JSON.stringify(evidence,null,2)}</pre></Modal>
    <Modal title="运行记录" open={!!audit} onCancel={()=>setAudit(null)} footer={null} width={860}><pre className="json-view">{JSON.stringify(audit,null,2)}</pre></Modal>
  </div>;
}
