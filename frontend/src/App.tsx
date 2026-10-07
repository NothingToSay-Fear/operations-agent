import {useCallback, useEffect, useRef, useState} from 'react';
import {Button, Drawer, Modal, Spin, message} from 'antd';
import {MenuOutlined} from '@ant-design/icons';
import {api, post, type Artifact, type ConversationTurn, type Task} from './api';
import {TOOL_LABELS} from './constants';
import AppDialogs from './components/AppDialogs';
import LoginPage from './components/LoginPage';
import OverviewDashboard from './components/OverviewDashboard';
import {MobileNavigation, Sidebar} from './components/Sidebar';
import TaskWorkspace from './components/TaskWorkspace';
import ResourceCenter, {SourcePreview} from './ResourceCenter';

type User = {
  id: string;
  username: string;
  is_admin?: boolean;
};

type AnswerPlayback = {
  taskId: string;
  previousAnswerId?: string;
};

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  const [conversation, setConversation] = useState<ConversationTurn[]>([]);
  const [overview, setOverview] = useState<any>(null);
  const [dataError, setDataError] = useState('');
  const [config, setConfig] = useState<any>(null);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [resourceOpen, setResourceOpen] = useState(false);
  const [resourceTab, setResourceTab] = useState('documents');
  const [pendingMemories, setPendingMemories] = useState(0);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState('');
  const [evidence, setEvidence] = useState<any>(null);
  const [audit, setAudit] = useState<any>(null);
  const [sourcePreview, setSourcePreview] = useState<any>(null);
  const [eventText, setEventText] = useState('');
  const [answerPlayback, setAnswerPlayback] = useState<AnswerPlayback | null>(null);
  const [mobileMenu, setMobileMenu] = useState(false);
  const [toast, toastHolder] = message.useMessage();
  const selectedTaskRef = useRef(selectedTaskId);

  selectedTaskRef.current = selectedTaskId;

  const showError = (error: unknown) => {
    void toast.error((error as Error).message);
  };

  const finishAnswerPlayback = useCallback(() => setAnswerPlayback(null), []);

  const loadTasks = async () => {
    setTasks(await api<Task[]>('/tasks'));
  };

  const refreshTask = async (taskId: string) => {
    const [nextTask, files, turns] = await Promise.all([
      api<Task>(`/tasks/${taskId}`),
      api<Artifact[]>(`/tasks/${taskId}/artifacts`),
      api<ConversationTurn[]>(`/tasks/${taskId}/conversation`),
    ]);

    if (selectedTaskRef.current === taskId) {
      setTask(nextTask);
      setArtifacts(files);
      setConversation(turns);
    }
  };

  const openResources = (tab: 'documents' | 'memory' | 'task') => {
    setResourceTab(tab);
    setResourceOpen(true);
    setMobileMenu(false);
  };

  const selectTask = (taskId: string | null) => {
    setSelectedTaskId(taskId);
    setInput('');
    setMobileMenu(false);
  };

  const logout = async () => {
    try {
      await post('/auth/logout', {});
      setUser(null);
      setSelectedTaskId(null);
      setTasks([]);
      setMobileMenu(false);
    } catch (error) {
      showError(error);
    }
  };

  const submit = async () => {
    const goal = input.trim();
    if (!goal) return;

    setBusy(true);
    try {
      if (selectedTaskId) {
        setAnswerPlayback({
          taskId: selectedTaskId,
          previousAnswerId: [...conversation].reverse().find((turn) => turn.role === 'assistant')?.id,
        });
        await post(`/tasks/${selectedTaskId}/control`, {action: 'message', message: goal});
        await refreshTask(selectedTaskId);
      } else {
        const created = await post<Task>('/tasks', {goal});
        setAnswerPlayback({taskId: created.id});
        setSelectedTaskId(created.id);
      }
      setInput('');
      await loadTasks();
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  };

  const controlTask = async (action: string) => {
    if (!selectedTaskId) return;
    try {
      if (action === 'resume') {
        setAnswerPlayback({
          taskId: selectedTaskId,
          previousAnswerId: [...conversation].reverse().find((turn) => turn.role === 'assistant')?.id,
        });
      }
      await post(`/tasks/${selectedTaskId}/control`, {action});
      await refreshTask(selectedTaskId);
      await loadTasks();
    } catch (error) {
      showError(error);
    }
  };

  const openEvidence = async (evidenceId: string) => {
    if (!selectedTaskId) return;
    try {
      setEvidence(await api(`/tasks/${selectedTaskId}/evidence/${evidenceId}`));
    } catch (error) {
      showError(error);
    }
  };

  const openArtifact = (artifactId: string) => {
    const nextArtifact = artifacts.find((item) => item.id === artifactId);
    if (!nextArtifact) {
      void toast.info('成果版本尚未加载，请稍后重试');
      return;
    }
    setArtifact(nextArtifact);
    setEditing(false);
  };

  const saveArtifact = async () => {
    if (!selectedTaskId || !artifact) return;
    try {
      await api(`/tasks/${selectedTaskId}/artifacts/${artifact.id}`, {
        method: 'PATCH',
        body: JSON.stringify({content: editText}),
      });
      await refreshTask(selectedTaskId);
      setArtifact(null);
      void toast.success('已保存新版本');
    } catch (error) {
      showError(error);
    }
  };

  const confirmDeleteTask = (taskId: string) => {
    Modal.confirm({
      title: '删除当前会话？',
      content: '消息、计划、运行记录、证据、成果、任务记忆、后台作业，以及由本会话确认生成的长期记忆都将永久删除。',
      okText: '永久删除',
      cancelText: '取消',
      okButtonProps: {danger: true},
      onOk: async () => {
        try {
          await api(`/tasks/${taskId}`, {method: 'DELETE'});
          setTasks((rows) => rows.filter((item) => item.id !== taskId));
          if (selectedTaskRef.current === taskId) {
            setSelectedTaskId(null);
            setTask(null);
            setArtifacts([]);
            setConversation([]);
            setArtifact(null);
            setEvidence(null);
            setAudit(null);
            setPendingMemories(0);
            setEventText('');
            setResourceOpen(false);
          }
          void toast.success('会话及相关内容已删除');
        } catch (error) {
          showError(error);
          throw error;
        }
      },
    });
  };

  useEffect(() => {
    api<User>('/auth/me').then(setUser).catch(() => {}).finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    if (!user) return;
    void loadTasks().catch(showError);
    api('/overview').then((data) => {
      setOverview(data);
      setDataError('');
    }).catch((error) => setDataError(error.message));
    api('/config').then(setConfig).catch(showError);
  }, [user]);

  useEffect(() => {
    setTask(null);
    setArtifacts([]);
    setConversation([]);
    setEventText('');
    setAnswerPlayback((current) => current?.taskId === selectedTaskId ? current : null);
    if (selectedTaskId) void refreshTask(selectedTaskId).catch(showError);
  }, [selectedTaskId]);

  useEffect(() => {
    if (!selectedTaskId || !user) {
      setPendingMemories(0);
      return;
    }

    const refresh = () => api<any[]>('/memory-candidates')
      .then((rows) => setPendingMemories(rows.filter((item) => item.task_id === selectedTaskId).length))
      .catch(() => {});
    void refresh();
    const timer = window.setInterval(() => void refresh(), 4_000);
    return () => window.clearInterval(timer);
  }, [selectedTaskId, user]);

  useEffect(() => {
    if (!selectedTaskId || !task || !['queued', 'running'].includes(task.status)) return;

    const taskId = selectedTaskId;
    const source = new EventSource(`/api/tasks/${taskId}/events?after=${task.state.seq ?? 0}`, {withCredentials: true});
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => {
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        void refreshTask(taskId).catch(showError);
        void loadTasks().catch(showError);
      }, 150);
    };

    source.addEventListener('update', (event) => {
      const data = JSON.parse((event as MessageEvent).data);
      const payload = data.payload || {};
      const toolText = Array.isArray(payload.tool)
        ? `正在并行执行：${payload.tool.map((name: string) => TOOL_LABELS[name] || name).join('、')}`
        : TOOL_LABELS[payload.tool];
      setEventText(payload.summary || payload.reason || payload.message || toolText || (
        data.kind === 'model_started' ? '正在分析当前目标与证据…' : '正在推进任务…'
      ));
      refresh();
    });
    source.addEventListener('done', () => { source.close(); refresh(); });
    source.onerror = () => { void refreshTask(taskId).catch(showError); };
    return () => {
      source.close();
      if (timer) window.clearTimeout(timer);
    };
  }, [selectedTaskId, task?.status]);

  if (checking) return <div className="full-loading"><Spin /></div>;
  if (!user) return <>{toastHolder}<LoginPage onLogin={(nextUser) => setUser(nextUser as User)} /></>;

  return (
    <div className="app-shell">
      {toastHolder}
      <Sidebar
        user={user}
        tasks={tasks}
        selectedTaskId={selectedTaskId}
        onSelectTask={selectTask}
        onOpenResources={openResources}
        onDeleteTask={confirmDeleteTask}
        onLogout={() => void logout()}
      />
      <main className="workspace">
        <header className="topbar">
          <Button className="mobile-nav-trigger" type="text" icon={<MenuOutlined />} aria-label="打开导航" onClick={() => setMobileMenu(true)} />
        </header>
        {selectedTaskId ? (
          <TaskWorkspace
            task={task}
            conversation={conversation}
            artifacts={artifacts}
            input={input}
            busy={busy}
            eventText={eventText}
            pendingMemories={pendingMemories}
            onInputChange={setInput}
            onSubmit={() => void submit()}
            onControl={(action) => void controlTask(action)}
            onOpenEvidence={(id) => void openEvidence(id)}
            onOpenArtifact={openArtifact}
            onOpenResource={openResources}
            onOpenAudit={() => selectedTaskId && api(`/tasks/${selectedTaskId}/audit`).then(setAudit).catch(showError)}
            answerPlayback={answerPlayback}
            onAnswerPlaybackComplete={finishAnswerPlayback}
          />
        ) : (
          <OverviewDashboard
            overview={overview}
            config={config}
            dataError={dataError}
            input={input}
            busy={busy}
            onInputChange={setInput}
            onSubmit={() => void submit()}
          />
        )}
      </main>

      <Drawer title="工作空间" className="mobile-drawer" placement="left" width={300} open={mobileMenu} onClose={() => setMobileMenu(false)}>
        <MobileNavigation
          tasks={tasks}
          selectedTaskId={selectedTaskId}
          onSelectTask={selectTask}
          onOpenResources={openResources}
          onDeleteTask={confirmDeleteTask}
          onLogout={() => void logout()}
        />
      </Drawer>

      <ResourceCenter
        open={resourceOpen}
        onClose={() => setResourceOpen(false)}
        initialTab={resourceTab}
        taskId={selectedTaskId}
        userId={user.id}
        isAdmin={Boolean(user.is_admin)}
      />
      <SourcePreview source={sourcePreview} onClose={() => setSourcePreview(null)} />
      <AppDialogs
        taskId={selectedTaskId}
        artifact={artifact}
        editing={editing}
        editText={editText}
        evidence={evidence}
        audit={audit}
        onCloseArtifact={() => setArtifact(null)}
        onToggleEditing={() => { setEditing(!editing); setEditText(artifact?.content || ''); }}
        onEditTextChange={setEditText}
        onSaveArtifact={() => void saveArtifact()}
        onCloseEvidence={() => setEvidence(null)}
        onOpenSource={setSourcePreview}
        onOpenHistory={(id) => selectedTaskId && api(`/tasks/${selectedTaskId}/history/${id}`).then(setEvidence).catch(showError)}
        onCloseAudit={() => setAudit(null)}
        onOpenEvidence={(id) => void openEvidence(id)}
        onOpenArtifact={openArtifact}
      />
    </div>
  );
}
