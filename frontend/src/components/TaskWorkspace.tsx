import {Alert, Button, Empty, Input, Spin, Tabs, Tag} from 'antd';
import {
  ArrowRightOutlined,
  ArrowUpOutlined,
  CheckOutlined,
  FileTextOutlined,
  LoadingOutlined,
  PauseOutlined,
  PlayCircleOutlined,
  StopOutlined,
} from '@ant-design/icons';
import type {Artifact, ConversationTurn, Task} from '../api';
import {STATUS_LABELS, TOOL_LABELS} from '../constants';
import ConversationMessage from './ConversationMessage';

type TaskWorkspaceProps = {
  task: Task | null;
  conversation: ConversationTurn[];
  artifacts: Artifact[];
  input: string;
  busy: boolean;
  eventText: string;
  pendingMemories: number;
  onInputChange: (value: string) => void;
  onSubmit: () => void;
  onControl: (action: string) => void;
  onOpenEvidence: (id: string) => void;
  onOpenArtifact: (id: string) => void;
  onOpenResource: (tab: 'memory' | 'task') => void;
  onOpenAudit: () => void;
  answerPlayback: {taskId: string; previousAnswerId?: string} | null;
  onAnswerPlaybackComplete: () => void;
};

export default function TaskWorkspace({
  task,
  conversation,
  artifacts,
  input,
  busy,
  eventText,
  pendingMemories,
  onInputChange,
  onSubmit,
  onControl,
  onOpenEvidence,
  onOpenArtifact,
  onOpenResource,
  onOpenAudit,
  answerPlayback,
  onAnswerPlaybackComplete,
}: TaskWorkspaceProps) {
  if (!task) {
    return <div className="task-layout"><section className="conversation"><div className="full-loading"><Spin /></div></section></div>;
  }

  const active = ['running', 'queued'].includes(task.status);
  const latestAssistant = conversation.at(-1)?.role === 'assistant' ? conversation.at(-1) : null;
  const earlierConversation = latestAssistant ? conversation.slice(0, -1) : conversation;
  const animateLatestAnswer = Boolean(
    latestAssistant
    && latestAssistant.kind === 'answer'
    && answerPlayback?.taskId === task.id
    && latestAssistant.id !== answerPlayback.previousAnswerId,
  );

  return (
    <div className="task-layout">
      <section className="conversation">
        <div className="task-heading">
          <div className="eyebrow">AGENT WORKSPACE</div>
          <div className="task-title-row">
            <h2>分析与执行</h2>
            <Tag color={task.status === 'completed' ? 'green' : active ? 'processing' : 'default'}>{STATUS_LABELS[task.status]}</Tag>
          </div>
        </div>

        <div className="messages">
          {earlierConversation.map((turn) => <ConversationMessage key={turn.id} turn={turn} onEvidence={onOpenEvidence} onArtifact={onOpenArtifact} />)}
          {task.state.plan && <PlanSummary task={task} />}
          {active && <div className="live-status"><LoadingOutlined spin /><span>{eventText || '正在理解目标并准备下一步…'}</span></div>}
          {task.state.waiting_question && task.status === 'waiting_user' && latestAssistant?.kind !== 'question' && (
            <Alert type="info" showIcon message="需要你补充一项信息" description={task.state.waiting_question} />
          )}
          {pendingMemories > 0 && (
            <Alert type="info" message={`有 ${pendingMemories} 条长期记忆等待确认，尚未生效`} action={<Button onClick={() => onOpenResource('memory')}>检查候选</Button>} />
          )}
          {latestAssistant && (
            <ConversationMessage
              turn={latestAssistant}
              onEvidence={onOpenEvidence}
              onArtifact={onOpenArtifact}
              animate={animateLatestAnswer}
              onAnimationComplete={onAnswerPlaybackComplete}
            />
          )}
          {artifacts.length > 0 && <ArtifactList artifacts={artifacts} onOpenArtifact={onOpenArtifact} />}
        </div>

        <TaskComposer
          task={task}
          input={input}
          busy={busy}
          onInputChange={onInputChange}
          onSubmit={onSubmit}
          onControl={onControl}
          onOpenAudit={onOpenAudit}
          onOpenTaskMemory={() => onOpenResource('task')}
        />
      </section>
      <ExecutionPanel task={task} onOpenEvidence={onOpenEvidence} />
    </div>
  );
}

function PlanSummary({task}: {task: Task}) {
  const plan = task.state.plan;
  if (!plan) return null;

  return (
    <div className="agent-message">
      <div className="agent-heading"><div className="mini-brand">序</div><strong>当前计划</strong><span>v{task.state.plans.length}</span></div>
      <p>{plan.summary}</p>
      {task.state.plans.length > 1 && <div className="replan-note">计划更新 · {plan.change_reason}</div>}
    </div>
  );
}

function ArtifactList({artifacts, onOpenArtifact}: {artifacts: Artifact[]; onOpenArtifact: (id: string) => void}) {
  return (
    <div className="artifact-list">
      {artifacts.map((artifact) => (
        <button key={artifact.id} className="artifact-card" onClick={() => onOpenArtifact(artifact.id)}>
          <FileTextOutlined />
          <span><strong>{artifact.title}</strong><small>{artifact.format.toUpperCase()} · 版本 {artifact.version}</small></span>
          <ArrowRightOutlined />
        </button>
      ))}
    </div>
  );
}

function TaskComposer({task, input, busy, onInputChange, onSubmit, onControl, onOpenAudit, onOpenTaskMemory}: {
  task: Task;
  input: string;
  busy: boolean;
  onInputChange: (value: string) => void;
  onSubmit: () => void;
  onControl: (action: string) => void;
  onOpenAudit: () => void;
  onOpenTaskMemory: () => void;
}) {
  const active = ['running', 'queued'].includes(task.status);
  const resumable = ['paused', 'blocked', 'failed', 'partial'].includes(task.status);
  const cancellable = !['completed', 'cancelled'].includes(task.status);

  return (
    <div className="task-composer-area">
      <div className="task-controls">
        {active && <Button size="small" icon={<PauseOutlined />} onClick={() => onControl('pause')}>暂停</Button>}
        {!active && resumable && <Button size="small" icon={<PlayCircleOutlined />} onClick={() => onControl('resume')}>继续任务</Button>}
        {cancellable && <Button size="small" type="text" icon={<StopOutlined />} onClick={() => onControl('cancel')}>取消任务</Button>}
        <button className="text-button audit-link" onClick={onOpenAudit}>查看运行记录</button>
      </div>
      <Button size="small" type="text" onClick={onOpenTaskMemory}>查看任务记忆与采用偏好</Button>
      <div className="composer">
        <Input.TextArea
          value={input}
          autoSize={{minRows: 2, maxRows: 5}}
          disabled={task.status === 'cancelled'}
          onChange={(event) => onInputChange(event.target.value)}
          placeholder="补充条件、调整目标，或回答 Agent 的问题…"
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              onSubmit();
            }
          }}
        />
        <div className="composer-footer">
          <span>修改条件后，将重新评估计划与证据</span>
          <Button type="primary" shape="circle" icon={<ArrowUpOutlined />} aria-label="发送补充条件" loading={busy} disabled={!input.trim() || task.status === 'cancelled'} onClick={onSubmit} />
        </div>
      </div>
    </div>
  );
}

function ExecutionPanel({task, onOpenEvidence}: {task: Task; onOpenEvidence: (id: string) => void}) {
  const plan = task.state.plan;
  return (
    <aside className="execution-panel">
      <Tabs items={[
        {
          key: 'plan',
          label: '执行计划',
          children: plan ? <PlanPanel task={task} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="计划将在这里展示" />,
        },
        {
          key: 'evidence',
          label: `证据 ${task.state.observations.length}`,
          children: <div className="evidence-list">{task.state.observations.map((observation, index) => (
            <button key={observation.evidence_id} onClick={() => onOpenEvidence(observation.evidence_id)}>
              <span className={`evidence-status ${observation.status}`} />
              <div><strong>{TOOL_LABELS[observation.tool] || observation.tool}</strong><small>观察 {index + 1} · {observation.status === 'success' ? '已获取' : observation.status === 'empty' ? '无数据' : '失败'}</small></div>
              <ArrowRightOutlined />
            </button>
          ))}</div>,
        },
      ]} />
      <div className="execution-footer"><span className="status-dot" /> 所有经营数据保持只读</div>
    </aside>
  );
}

function PlanPanel({task}: {task: Task}) {
  const plan = task.state.plan!;
  return (
    <>
      <div className="plan-caption">根据观察持续调整 <Tag>v{task.state.plans.length}</Tag></div>
      <ol className="plan-list">
        {plan.steps.map((step, index) => {
          const status = task.state.steps[step.id]?.status;
          const done = status === 'succeeded';
          const skipped = status === 'skipped';
          return (
            <li key={step.id} className={done ? 'done' : ''}>
              <div className="step-number">{done ? <CheckOutlined /> : skipped ? '—' : index + 1}</div>
              <div>
                <strong>{step.objective}</strong>
                <p>{step.done_when}</p>
                {(done || skipped) && <span className="step-complete">{done ? '已完成' : '目标已满足，无需继续'}</span>}
              </div>
            </li>
          );
        })}
      </ol>
      <div className="success-criteria"><h4>完成标准</h4>{plan.criteria.map((criterion) => <p key={criterion.id}>◌ {criterion.description}</p>)}</div>
    </>
  );
}
