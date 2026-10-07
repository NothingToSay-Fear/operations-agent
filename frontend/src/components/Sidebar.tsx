import {Button, Tooltip} from 'antd';
import {
  BarChartOutlined,
  BookOutlined,
  BulbOutlined,
  DeleteOutlined,
  LogoutOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import type {Task} from '../api';

type User = {username: string};

type NavigationProps = {
  tasks: Task[];
  selectedTaskId: string | null;
  onSelectTask: (id: string | null) => void;
  onOpenResources: (tab: 'documents' | 'memory') => void;
  onDeleteTask: (id: string) => void;
};

function TaskList({tasks, selectedTaskId, onSelectTask, onDeleteTask}: Omit<NavigationProps, 'onOpenResources'>) {
  if (!tasks.length) {
    return <p className="muted small">任务会保存在这里</p>;
  }

  return (
    <div className="task-list">
      {tasks.map((task) => (
        <div key={task.id} className={`task-row ${selectedTaskId === task.id ? 'selected' : ''}`}>
          <button className="task-link" onClick={() => onSelectTask(task.id)}>
            <span className={`task-indicator ${task.status}`} />
            <span>{task.goal}</span>
          </button>
          <Tooltip title="删除会话">
            <Button
              className="task-delete"
              type="text"
              danger
              size="small"
              icon={<DeleteOutlined />}
              aria-label={`删除会话：${task.goal}`}
              onClick={() => onDeleteTask(task.id)}
            />
          </Tooltip>
        </div>
      ))}
    </div>
  );
}

export function Sidebar({
  user,
  tasks,
  selectedTaskId,
  onSelectTask,
  onOpenResources,
  onDeleteTask,
  onLogout,
}: NavigationProps & {user: User; onLogout: () => void}) {
  return (
    <aside className="sidebar">
      <Button className="new-task" type="primary" icon={<PlusOutlined />} onClick={() => onSelectTask(null)}>发起新任务</Button>
      <button className={`nav-item ${!selectedTaskId ? 'active' : ''}`} onClick={() => onSelectTask(null)}><BarChartOutlined /> 运营总览</button>
      <button className="nav-item" onClick={() => onOpenResources('documents')}><BookOutlined /> 运营资料 <span className="nav-arrow">→</span></button>
      <button className="nav-item" onClick={() => onOpenResources('memory')}><BulbOutlined /> 长期记忆</button>
      <div className="sidebar-label">最近任务 <span>{tasks.length}</span></div>
      <TaskList tasks={tasks} selectedTaskId={selectedTaskId} onSelectTask={onSelectTask} onDeleteTask={onDeleteTask} />
      <div className="sidebar-bottom">
        <div className="profile">
          <div className="avatar">{user.username.slice(0, 1).toUpperCase()}</div>
          <span>{user.username}</span>
          <Tooltip title="退出登录">
            <Button type="text" icon={<LogoutOutlined />} aria-label="退出登录" onClick={onLogout} />
          </Tooltip>
        </div>
      </div>
    </aside>
  );
}

export function MobileNavigation({
  tasks,
  selectedTaskId,
  onSelectTask,
  onOpenResources,
  onDeleteTask,
  onLogout,
}: NavigationProps & {onLogout: () => void}) {
  return (
    <>
      <button className="nav-item" onClick={() => onSelectTask(null)}><BarChartOutlined />运营总览</button>
      <button className="nav-item" onClick={() => onOpenResources('documents')}><BookOutlined />运营资料</button>
      <button className="nav-item" onClick={() => onOpenResources('memory')}><BulbOutlined />长期记忆</button>
      <p className="muted small">最近任务</p>
      <TaskList tasks={tasks} selectedTaskId={selectedTaskId} onSelectTask={onSelectTask} onDeleteTask={onDeleteTask} />
      <Button type="text" icon={<LogoutOutlined />} onClick={onLogout}>退出登录</Button>
    </>
  );
}
