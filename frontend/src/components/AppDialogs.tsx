import {Button, Input, Modal} from 'antd';
import {DownloadOutlined, EditOutlined} from '@ant-design/icons';
import type {Artifact} from '../api';
import Markdown from './Markdown';

type AppDialogsProps = {
  taskId: string | null;
  artifact: Artifact | null;
  editing: boolean;
  editText: string;
  evidence: any;
  audit: any;
  onCloseArtifact: () => void;
  onToggleEditing: () => void;
  onEditTextChange: (value: string) => void;
  onSaveArtifact: () => void;
  onCloseEvidence: () => void;
  onOpenSource: (source: any) => void;
  onOpenHistory: (id: string) => void;
  onCloseAudit: () => void;
  onOpenEvidence: (id: string) => void;
  onOpenArtifact: (id: string) => void;
};

export default function AppDialogs({
  taskId,
  artifact,
  editing,
  editText,
  evidence,
  audit,
  onCloseArtifact,
  onToggleEditing,
  onEditTextChange,
  onSaveArtifact,
  onCloseEvidence,
  onOpenSource,
  onOpenHistory,
  onCloseAudit,
  onOpenEvidence,
  onOpenArtifact,
}: AppDialogsProps) {
  const documentRows = evidence?.result?.data?.rows?.filter((row: any) => row.document_id) || [];
  const historyRows = evidence?.result?.data?.rows?.filter((row: any) => row.task_id) || [];

  return (
    <>
      <Modal
        title={artifact ? `${artifact.title} · v${artifact.version}` : ''}
        open={Boolean(artifact)}
        onCancel={onCloseArtifact}
        width={900}
        footer={artifact ? <>
          <Button icon={<DownloadOutlined />} href={`/api/tasks/${taskId}/artifacts/${artifact.id}/download`}>下载</Button>
          <Button icon={<EditOutlined />} onClick={onToggleEditing}>{editing ? '取消编辑' : '编辑副本'}</Button>
          {editing && <Button type="primary" onClick={onSaveArtifact}>保存新版本</Button>}
        </> : null}
      >
        {artifact && (editing
          ? <Input.TextArea rows={20} value={editText} onChange={(event) => onEditTextChange(event.target.value)} />
          : artifact.format === 'csv'
            ? <pre className="json-view">{artifact.content}</pre>
            : <Markdown text={artifact.content} onEvidence={onOpenEvidence} onArtifact={onOpenArtifact} />)}
      </Modal>

      <Modal title="证据详情" open={Boolean(evidence)} onCancel={onCloseEvidence} footer={null} width={860}>
        {documentRows.map((row: any) => <Button key={row.id} onClick={() => onOpenSource(row)}>{row.title} · {row.heading || '查看原文'}</Button>)}
        {historyRows.map((row: any) => <Button key={row.id} onClick={() => onOpenHistory(row.id)}>当前任务历史 · {row.seq}</Button>)}
        <pre className="json-view">{JSON.stringify(evidence, null, 2)}</pre>
      </Modal>

      <Modal title="运行记录" open={Boolean(audit)} onCancel={onCloseAudit} footer={null} width={860}>
        <pre className="json-view">{JSON.stringify(audit, null, 2)}</pre>
      </Modal>
    </>
  );
}
