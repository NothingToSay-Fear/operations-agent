import {useEffect, useRef} from 'react';
import ReactMarkdown, {defaultUrlTransform} from 'react-markdown';
import remarkGfm from 'remark-gfm';

type TargetType = 'ev' | 'ar';

type MarkdownProps = {
  text: string;
  onEvidence?: (id: string) => void;
  onArtifact?: (id: string) => void;
};

function normalizeReferenceLinks(text: string) {
  // 将旧回答里由模型生成的页面锚点统一为内部链接，避免浏览器直接跳转页面。
  const referencePattern = '(ev|ar)_[a-zA-Z0-9_-]+';
  const markdownLink = new RegExp(
    `\\[([^\\]]*)\\]\\((?:https?:\\/\\/[^)\\s]*#|#|evidence:|artifact:)(${referencePattern})\\)`,
    'g',
  );
  const bareUrl = new RegExp(`https?:\\/\\/[^\\s)<]*#(${referencePattern})\\b`, 'g');

  return text
    .replace(markdownLink, (_match, label, referenceId) => {
      const scheme = referenceId.startsWith('ev_') ? 'evidence' : 'artifact';
      return `[${label}](${scheme}:${referenceId})`;
    })
    .replace(bareUrl, (_match, referenceId) => {
      const scheme = referenceId.startsWith('ev_') ? 'evidence' : 'artifact';
      return `[${referenceId}](${scheme}:${referenceId})`;
    });
}

function linkEvidenceReferences(text: string) {
  const normalizedText = normalizeReferenceLinks(text);
  const protectedFragments: string[] = [];
  const protectedText = normalizedText.replace(/```[\s\S]*?```|`[^`\r\n]*`|\[[^\]]*\]\([^\)\r\n]*\)/g, (fragment) => {
    const index = protectedFragments.push(fragment) - 1;
    return `\uE000${index}\uE001`;
  });
  const linkedText = protectedText.replace(/\bev_[a-zA-Z0-9_-]+\b/g, (evidenceId) => (
    `[${evidenceId}](evidence:${evidenceId})`
  ));

  return linkedText.replace(/\uE000(\d+)\uE001/g, (_, index) => protectedFragments[Number(index)]);
}

function targetId(href: string | undefined, type: TargetType) {
  const prefix = type === 'ev' ? 'evidence:' : 'artifact:';
  const match = decodeURIComponent(href || '').match(new RegExp(`(?:^${prefix}|#)(${type}_[a-zA-Z0-9_-]+)$`));
  return match?.[1];
}

function referenceInElement(element: Element) {
  const anchor = element.closest('a');
  for (const type of ['ev', 'ar'] as const) {
    const id = targetId(anchor?.getAttribute('href') || undefined, type);
    if (id) return {id, type};
  }
  const text = element.closest('a, button, code')?.textContent || '';
  const match = text.match(/\b(ev|ar)_[a-zA-Z0-9_-]+\b/);
  return match ? {id: match[0], type: match[1] as TargetType} : undefined;
}

function CitationButton({id, type}: {id: string; type: TargetType}) {
  const label = type === 'ev' ? '查看证据' : '查看成果';
  return (
    <button type="button" className="citation-button">
      <span>{label}</span>
      <code>{id}</code>
    </button>
  );
}

export default function Markdown({text, onEvidence, onArtifact}: MarkdownProps) {
  const linkedText = linkEvidenceReferences(text);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;

    // 在浏览器默认跳转前捕获旧锚点与新按钮，统一打开应用内详情弹窗。
    const openReference = (event: MouseEvent) => {
      if (!(event.target instanceof Element) || !root.contains(event.target)) return;
      const reference = referenceInElement(event.target);
      if (!reference) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (reference.type === 'ev') onEvidence?.(reference.id);
      else onArtifact?.(reference.id);
    };
    root.addEventListener('click', openReference, true);
    return () => root.removeEventListener('click', openReference, true);
  }, [onArtifact, onEvidence]);

  return (
    <div ref={rootRef} className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={(url) => (
          /^(evidence:ev_|artifact:ar_)[a-zA-Z0-9_-]+$/.test(url)
            ? url
            : defaultUrlTransform(url)
        )}
        components={{
          a: ({href, children}) => {
            const evidenceId = targetId(href, 'ev');
            const artifactId = targetId(href, 'ar');

            if (evidenceId) {
              return <CitationButton id={evidenceId} type="ev" />;
            }
            if (artifactId) {
              return <CitationButton id={artifactId} type="ar" />;
            }
            return <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>;
          },
          code: ({children, node: _node, ...props}) => {
            const referenceId = String(children).trim();
            if (/^ev_[a-zA-Z0-9_-]+$/.test(referenceId)) {
              return <CitationButton id={referenceId} type="ev" />;
            }
            if (/^ar_[a-zA-Z0-9_-]+$/.test(referenceId)) {
              return <CitationButton id={referenceId} type="ar" />;
            }
            return <code {...props}>{children}</code>;
          },
        }}
      >
        {linkedText}
      </ReactMarkdown>
    </div>
  );
}
