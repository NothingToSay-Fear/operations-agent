import {useEffect, useRef, useState} from 'react';
import type {ConversationTurn} from '../api';
import Markdown from './Markdown';

type ConversationMessageProps = {
  turn: ConversationTurn;
  onEvidence: (id: string) => void;
  onArtifact: (id: string) => void;
  animate?: boolean;
  onAnimationComplete?: () => void;
};

export default function ConversationMessage({
  turn,
  onEvidence,
  onArtifact,
  animate = false,
  onAnimationComplete,
}: ConversationMessageProps) {
  const [displayedContent, setDisplayedContent] = useState(turn.content);
  const animationFrame = useRef<number>();

  useEffect(() => {
    window.cancelAnimationFrame(animationFrame.current || 0);

    if (!animate || turn.role !== 'assistant') {
      setDisplayedContent(turn.content);
      return;
    }

    setDisplayedContent('');
    const duration = Math.min(2200, Math.max(400, turn.content.length * 7));
    const startedAt = performance.now();
    const play = (now: number) => {
      const progress = Math.min(1, (now - startedAt) / duration);
      const length = Math.ceil(turn.content.length * progress);
      setDisplayedContent(turn.content.slice(0, length));
      if (progress < 1) {
        animationFrame.current = window.requestAnimationFrame(play);
      } else {
        onAnimationComplete?.();
      }
    };
    animationFrame.current = window.requestAnimationFrame(play);

    return () => window.cancelAnimationFrame(animationFrame.current || 0);
  }, [animate, onAnimationComplete, turn.content, turn.role]);

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
      <Markdown text={displayedContent} onEvidence={onEvidence} onArtifact={onArtifact} />
      {animate && displayedContent.length < turn.content.length && <span className="typewriter-cursor" aria-label="正在展示回答" />}
      <div className="answer-usage">本轮模型调用 {turn.model_calls ?? 0} 次</div>
    </div>
  );
}
