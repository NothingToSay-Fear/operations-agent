type TrendRow = {group: string; paid_gmv: number};

export default function TrendChart({rows}: {rows: TrendRow[]}) {
  if (rows.length < 2) {
    return <div className="chart-empty">暂无趋势数据</div>;
  }

  const width = 640;
  const height = 130;
  const maximum = Math.max(...rows.map((row) => row.paid_gmv), 1);
  const points = rows
    .map((row, index) => `${index / (rows.length - 1) * width},${height - row.paid_gmv / maximum * (height - 15)}`)
    .join(' ');

  return (
    <div className="trend">
      <svg viewBox={`0 0 ${width} ${height + 6}`} preserveAspectRatio="none" role="img" aria-label="近十四天支付商品 GMV 趋势">
        <defs>
          <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#b5e899" stopOpacity=".2" />
            <stop offset="100%" stopColor="#b5e899" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[25, 70, 115].map((position) => (
          <line key={position} x1="0" x2={width} y1={position} y2={position} stroke="#ffffff0a" />
        ))}
        <polygon points={`0,${height} ${points} ${width},${height}`} fill="url(#fill)" />
        <polyline points={points} fill="none" stroke="#b5e899" strokeWidth="2.5" strokeLinejoin="round" />
      </svg>
      <div className="chart-axis">
        <span>{rows[0].group}</span>
        <span>按支付日 · 不含运费</span>
        <span>{rows[rows.length - 1].group}</span>
      </div>
    </div>
  );
}
