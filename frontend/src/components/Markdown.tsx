import ReactMarkdown, {defaultUrlTransform} from 'react-markdown';
import remarkGfm from 'remark-gfm';

type TargetType = 'ev' | 'ar';

type MarkdownProps = {
  text: string;
  onEvidence?: (id: string) => void;
  onArtifact?: (id: string) => void;
};

function linkEvidenceReferences(text: string) {
  const protectedFragments: string[] = [];
  const protectedText = text.replace(/```[\s\S]*?```|`[^`\r\n]*`|\[[^\]]*\]\([^\)\r\n]*\)/g, (fragment) => {
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
  const match = href?.match(new RegExp(`(?:^${prefix}|#)(${type}_[a-zA-Z0-9_-]+)$`));
  return match?.[1];
}

export default function Markdown({text, onEvidence, onArtifact}: MarkdownProps) {
  const linkedText = linkEvidenceReferences(text);

  return (
    <div className="markdown">
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
              return <button type="button" className="inline-link" onClick={() => onEvidence?.(evidenceId)}>{children}</button>;
            }
            if (artifactId) {
              return <button type="button" className="inline-link" onClick={() => onArtifact?.(artifactId)}>{children}</button>;
            }
            return <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>;
          },
          code: ({children, node: _node, ...props}) => {
            const referenceId = String(children).trim();
            if (/^ev_[a-zA-Z0-9_-]+$/.test(referenceId)) {
              return <button type="button" className="inline-link" onClick={() => onEvidence?.(referenceId)}>{referenceId}</button>;
            }
            if (/^ar_[a-zA-Z0-9_-]+$/.test(referenceId)) {
              return <button type="button" className="inline-link" onClick={() => onArtifact?.(referenceId)}>{referenceId}</button>;
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
