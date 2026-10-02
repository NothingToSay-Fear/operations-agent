import {useState} from 'react';
import {Alert, Button, Input} from 'antd';
import {ArrowRightOutlined} from '@ant-design/icons';
import {post} from '../api';
import Brand from './Brand';

type LoginPageProps = {
  onLogin: (user: unknown) => void;
};

export default function LoginPage({onLogin}: LoginPageProps) {
  const [register, setRegister] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    setBusy(true);
    setError('');
    try {
      const user = await post(`/auth/${register ? 'register' : 'login'}`, {username, password});
      onLogin(user);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-story">
        <Brand />
        <div>
          <div className="eyebrow">从经营问题，到有依据的行动</div>
          <h1>
            把目标交给 Agent。<br />
            <em>让证据指引下一步。</em>
          </h1>
          <p>
            理解目标，自主规划，持续调查。<br />
            在商品、订单、库存与活动之间，找到值得采取的行动。
          </p>
        </div>
        <div className="story-footer"><span className="status-dot" /> 模拟经营数据 · 只读分析与建议</div>
      </section>

      <section className="login-form">
        <div className="eyebrow">你的运营工作空间</div>
        <h2>{register ? '创建账号' : '欢迎回来'}</h2>
        <p>继续你的分析与决策。</p>
        <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
          <label>
            用户名
            <Input
              size="large"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="至少 3 个字符"
              autoComplete="username"
            />
          </label>
          <label>
            密码
            <Input.Password
              size="large"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="至少 8 个字符"
              autoComplete={register ? 'new-password' : 'current-password'}
            />
          </label>
          {error && <Alert type="error" message={error} showIcon />}
          <Button htmlType="submit" type="primary" size="large" block loading={busy}>
            {register ? '创建并进入' : '进入工作台'} <ArrowRightOutlined />
          </Button>
        </form>
        <button className="text-button" onClick={() => { setRegister(!register); setError(''); }}>
          {register ? '已有账号，返回登录' : '首次使用？创建一个账号'}
        </button>
      </section>
    </main>
  );
}
