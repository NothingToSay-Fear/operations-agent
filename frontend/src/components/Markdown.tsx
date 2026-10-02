import ReactMarkdown, {defaultUrlTransform} from 'react-markdown';
import remarkGfm from 'remark-gfm';

type TargetType = 'ev' | 'ar';

type MarkdownProps = {
  text: string;
  onEvidence?: (id: string) => void;
  onArtifact?: (id: string) => void;
};

function targetId(href: string | undefined, type: TargetType) {
  const prefix = type === 'ev' ? 'evidence:' : 'artifact:';
  const match = href?.match(new RegExp(`(?:^${prefix}|#)(${type}_[a-zA-Z0-9_-]+)$`));
  return match?.[1];
}

export default function Markdown({text, onEvidence, onArtifact}: MarkdownProps) {
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
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
