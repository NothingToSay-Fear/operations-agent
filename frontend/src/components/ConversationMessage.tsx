import type {ConversationTurn} from '../api';
import Markdown from './Markdown';

type ConversationMessageProps = {
  turn: ConversationTurn;
  onEvidence: (id: string) => void;
  onArtifact: (id: string) => void;
};

export default function ConversationMessage({
  turn,
  onEvidence,
  onArtifact,
}: ConversationMessageProps) {
  if (turn.role === 'user') {
    return (
      <div className="user-message">
        <span className="message-caption">{turn.kind === 'goal' ? '任务目标' : '追问'}</span>
        <p>{turn.content}</p>
      </div>
    );
  }

  const title = turn.kind === 'question'
    ? '需要补充'
    : turn.kind === 'notice'
      ? '当前进展'
      : '分析成果';

  return (
    <div className="agent-message answer">
      <div className="agent-heading">
        <div className="mini-brand">序</div>
        <strong>{title}</strong>
        {turn.context_outdated && (
          <span className="context-outdated">历史记录：当前资料或偏好已变化</span>
        )}
      </div>
      <Markdown text={turn.content} onEvidence={onEvidence} onArtifact={onArtifact} />
      <div className="answer-usage">本轮模型调用 {turn.model_calls ?? 0} 次</div>
    </div>
  );
}
