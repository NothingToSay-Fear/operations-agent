export async function api<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    credentials: 'include',
    ...options,
    headers: {
      ...(options.body instanceof FormData ? {} : {'Content-Type': 'application/json'}),
      ...options.headers,
    },
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = error.detail;
    const message = typeof detail === 'string'
      ? detail
      : Array.isArray(detail)
        ? detail.map((item: {msg?: string}) => item.msg).filter(Boolean).join('；')
        : `请求失败 (${response.status})`;
    throw new Error(message);
  }

  return response.json();
}

export const post = <T = unknown>(path: string, body: unknown) => api<T>(path, {
  method: 'POST',
  body: JSON.stringify(body),
});

export type PlanStep = {
  id: string;
  objective: string;
  depends_on: string[];
  done_when: string;
};

export type Task = {
  id: string;
  goal: string;
  status: string;
  revision: number;
  created_at: number;
  updated_at: number;
  state: {
    messages: {role: string; content: string}[];
    plan: null | {
      summary: string;
      criteria: {id: string; description: string}[];
      steps: PlanStep[];
      change_reason: string;
    };
    seq: number;
    plans: unknown[];
    steps: Record<string, {status: string; summary?: string}>;
    observations: Array<{evidence_id: string; status: string; tool: string}>;
    usage: Record<string, number | null>;
    turn_usage: Record<string, number>;
    turn_number: number;
    budget: Record<string, number>;
    artifacts: unknown[];
    answer: string;
    waiting_question: string;
    feedback: string;
    context_outdated?: boolean;
  };
};

export type Artifact = {
  id: string;
  title: string;
  format: string;
  content: string;
  version: number;
  evidence_ids: string[];
};

export type ConversationTurn = {
  id: string;
  seq: number;
  role: 'user' | 'assistant';
  kind: 'goal' | 'message' | 'answer' | 'question' | 'notice';
  content: string;
  created_at: number;
  model_calls?: number;
  context_outdated?: boolean;
};
