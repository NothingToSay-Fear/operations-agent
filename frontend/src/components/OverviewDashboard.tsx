import {Alert, Button, Input} from 'antd';
import {ArrowRightOutlined, ArrowUpOutlined} from '@ant-design/icons';
import {SUGGESTIONS, formatNumber} from '../constants';
import TrendChart from './TrendChart';

type OverviewDashboardProps = {
  overview: any;
  config: any;
  dataError: string;
  input: string;
  busy: boolean;
  onInputChange: (value: string) => void;
  onSubmit: () => void;
};

const metrics = [
  {label: '支付商品 GMV', key: 'paid_gmv', prefix: '¥', digits: 0},
  {label: '支付订单', key: 'paid_orders', prefix: '', digits: 0},
  {label: '商品客单价', key: 'aov', prefix: '¥', digits: 2},
  {label: '退款到账额', key: 'refund_amount', prefix: '¥', digits: 0},
];

export default function OverviewDashboard({
  overview,
  config,
  dataError,
  input,
  busy,
  onInputChange,
  onSubmit,
}: OverviewDashboardProps) {
  const current = overview?.current.rows[0];
  const previous = overview?.previous.rows[0];

  return (
    <div className="overview page-enter">
      <div className="heading-row">
        <div>
          <div className="eyebrow">YOUR OPERATIONS, IN FOCUS</div>
          <h1>看清经营，决定下一步<span className="lime">。</span></h1>
          <p>从一个问题开始，让 Agent 规划、调查并交付有依据的建议。</p>
        </div>
        <div className="date-block">
          <span>数据截止</span>
          <strong>{overview?.dataset.as_of || '—'}</strong>
          <small>Asia / Shanghai</small>
        </div>
      </div>

      {dataError && <Alert type="warning" message={dataError} showIcon className="notice" />}
      {config && !config.model_ready && (
        <Alert
          type="info"
          message="模型尚未连接。经营数据可以查看，分析任务会保留并等待模型配置。"
          showIcon
          className="notice"
        />
      )}

      <div className="metric-grid">
        {metrics.map((metric) => {
          const before = previous?.[metric.key];
          const now = current?.[metric.key];
          const delta = before ? (now - before) / before * 100 : null;
          return (
            <div className="metric-card" key={metric.key}>
              <div className="metric-label">{metric.label}<span>近 7 天</span></div>
              <strong><small>{metric.prefix}</small>{formatNumber(now, metric.digits)}</strong>
              <div className="metric-comparison">
                <span className={delta !== null && delta >= 0 ? 'positive' : 'neutral'}>
                  {delta === null ? '—' : `${delta >= 0 ? '↑' : '↓'} ${Math.abs(delta).toFixed(1)}%`}
                </span>
                <span>较前 7 天</span>
              </div>
            </div>
          );
        })}
      </div>

      <section className="chart-panel">
        <div className="panel-title">
          <span><span className="status-dot" /> 销售趋势</span>
          <span className="muted small">近 14 天 / 支付商品 GMV</span>
        </div>
        <TrendChart rows={overview?.trend.rows || []} />
      </section>

      <div className="section-heading">
        <h2>今天想解决什么问题？</h2>
        <span>给出目标，Agent 自主推进</span>
      </div>
      <div className="suggestion-grid">
        {SUGGESTIONS.map((suggestion) => (
          <button
            className="suggestion"
            key={suggestion.tag}
            disabled={busy}
            onClick={() => onInputChange(suggestion.goal)}
          >
            <div className="suggestion-top"><span>{suggestion.icon}</span><ArrowRightOutlined /></div>
            <small>{suggestion.tag}</small>
            <strong>{suggestion.title}</strong>
          </button>
        ))}
      </div>

      <div className="composer home-composer">
        <Input.TextArea
          autoSize={{minRows: 2, maxRows: 6}}
          value={input}
          onChange={(event) => onInputChange(event.target.value)}
          placeholder="描述你的目标，例如：最近销售波动的主要贡献项是什么？"
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              onSubmit();
            }
          }}
        />
        <div className="composer-footer">
          <span><span className="tiny-orbit" /> 自主规划 · 持续验证 · 证据可追溯</span>
          <Button type="primary" shape="circle" icon={<ArrowUpOutlined />} aria-label="开始分析" loading={busy} disabled={!input.trim()} onClick={onSubmit} />
        </div>
      </div>
      <p className="footnote">所有输出为建议，不会修改商品、库存或投放。</p>
    </div>
  );
}
