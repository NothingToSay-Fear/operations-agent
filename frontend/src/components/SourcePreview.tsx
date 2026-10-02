import {useEffect, useState} from 'react';
import {Alert, Button, Modal} from 'antd';
import {api} from '../api';

type SourcePreviewProps = {
  source: any;
  onClose: () => void;
};

export default function SourcePreview({source, onClose}: SourcePreviewProps) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    setData(null);
    setError('');
    if (!source) return;

    const params = new URLSearchParams();
    if (source.version_id) params.set('version_id', source.version_id);
    if (source.id && source.document_id && source.id !== source.document_id) {
      params.set('segment_id', source.id);
    }
    api(`/documents/${source.document_id || source.id}/content?${params}`)
      .then(setData)
      .catch((reason) => setError(reason.message));
  }, [source]);

  const loadMore = async () => {
    try {
      const next = await api<any>(`/documents/${data.id}/content?version_id=${data.version_id}&position=${data.position + 12000}`);
      setData({...next, text: data.text + next.text});
    } catch (reason) {
      setError((reason as Error).message);
    }
  };

  return (
    <Modal title={data?.title || '资料原文'} open={Boolean(source)} onCancel={onClose} footer={null} width={850}>
      {error ? <Alert type="warning" message={error} /> : (
        <>
          <p className="muted">版本 {data?.version || '—'} · {data?.heading || '原文'} {data?.location?.page_start ? `· 第 ${data.location.page_start} 页` : ''}</p>
          <pre className="source-text">{data?.text || '正在读取…'}</pre>
          {data?.has_more && <Button onClick={() => void loadMore()}>继续读取</Button>}
        </>
      )}
    </Modal>
  );
}
