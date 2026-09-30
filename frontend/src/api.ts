export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, {credentials: 'include', ...options,
    headers: {...(!(options.body instanceof FormData) ? {'Content-Type': 'application/json'} : {}), ...options.headers}});
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = error.detail;
    throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((e: any) => e.msg).join('；') : `请求失败 (${response.status})`);
  }
  return response.json();
}

export const post = <T = any>(path: string, body: unknown) => api<T>(path, {method: 'POST', body: JSON.stringify(body)});

export type Task = {id: string; goal: string; status: string; revision: number; created_at: number; updated_at: number; state: {
  messages: {role: string; content: string}[];
  plan: null | {summary: string; criteria: {id: string; description: string}[]; steps: {id: string; objective: string; depends_on: string[]; done_when: string}[]; change_reason: string};
  seq: number; plans: any[]; steps: Record<string, {status: string; summary?: string}>; observations: any[];
  usage: Record<string, number | null>; turn_usage: Record<string, number>; turn_number: number;
  budget: Record<string, number>; artifacts: any[];
  answer: string; waiting_question: string; feedback: string;
}};

export type Artifact = {id: string; title: string; format: string; content: string; version: number; evidence_ids: string[]};

export type ConversationTurn = {id: string; seq: number; role: 'user'|'assistant'; kind: 'goal'|'message'|'answer'|'question'|'notice'; content: string; created_at: number};
