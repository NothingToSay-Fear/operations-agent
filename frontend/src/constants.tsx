import {
  BarChartOutlined,
  BulbOutlined,
  EditOutlined,
} from '@ant-design/icons';

export const STATUS_LABELS: Record<string, string> = {
  queued: '待执行',
  running: '执行中',
  waiting_user: '等待补充',
  paused: '已暂停',
  completed: '已完成',
  partial: '部分完成',
  blocked: '待处理',
  failed: '执行失败',
  cancelled: '已取消',
};

export const TOOL_LABELS: Record<string, string> = {
  inspect_data_capabilities: '了解数据范围',
  get_metric_definitions: '核对指标口径',
  query_metrics: '查询经营指标',
  compare_metrics: '对比周期与贡献',
  get_products: '读取商品信息',
  query_order_facts: '调查订单与退款',
  query_inventory: '检查库存与在途',
  query_marketing: '分析渠道与活动',
  search_knowledge: '检索运营资料',
  read_document: '阅读资料原文',
  calculate: '验证计算',
  save_artifact: '保存工作成果',
  read_evidence: '复核已有证据',
};

export const SUGGESTIONS = [
  {
    tag: '经营诊断',
    title: '销售变化，找到关键贡献项',
    icon: <BarChartOutlined />,
    goal: '分析数据截止日期之前七天的GMV，相比前七天下降或增长的主要贡献项是什么？结合渠道和商品数据，给出三个有证据的运营建议。',
  },
  {
    tag: '库存决策',
    title: '提前发现补货与缺货风险',
    icon: <span>◌</span>,
    goal: '根据最新库存、在途和销售情况，找出未来两周有缺货风险的SKU，给出补货优先级和数量建议。明确交期、最小订货量和需求假设。',
  },
  {
    tag: '活动方案',
    title: '让活动兼顾销量与毛利',
    icon: <BulbOutlined />,
    goal: '结合现有商品成本、库存与活动规则，制定未来一周的促销建议，说明参与商品、折扣、毛利约束和执行前需要验证的信息，保存一份方案。',
  },
  {
    tag: '商品优化',
    title: '用准确的卖点改善商品表达',
    icon: <EditOutlined />,
    goal: '读取前五个SKU的商品信息和品牌文案规范，优化商品标题与卖点，保留可验证的属性，不虚构材质或功效，保存文案草稿。',
  },
];

export function formatNumber(value: number | null | undefined, digits = 0) {
  return value == null
    ? '—'
    : value.toLocaleString('zh-CN', {maximumFractionDigits: digits});
}
