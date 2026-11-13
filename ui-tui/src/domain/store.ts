import { atom } from 'nanostores';

export interface SessionInfo {
  sessionId: string;
  model: string;
  workspaceRoot: string;
}

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system' | string;
  content: string;
  timestamp: number;
}

export interface ToolCall {
  callId: string;
  name: string;
  args: any;
  status: 'running' | 'completed' | 'error';
  output?: any;
}

export interface ApprovalRequest {
  id: string;
  tool: string;
  args: any;
  diff?: string;
}

export interface StatusInfo {
  ready: boolean;
  thinking: boolean;
  tokens: number;
  model: string;
}

// Initial default states
const initialSession: SessionInfo = {
  sessionId: '',
  model: 'default',
  workspaceRoot: process.cwd(),
};

const initialStatus: StatusInfo = {
  ready: false,
  thinking: false,
  tokens: 0,
  model: 'default',
};

// Core Nano Stores atoms
export const $session = atom<SessionInfo>(initialSession);
export const $messages = atom<Message[]>([]);
export const $activeToolCalls = atom<ToolCall[]>([]);
export const $approval = atom<ApprovalRequest | null>(null);
export const $status = atom<StatusInfo>(initialStatus);

// Domain Actions
export function setSession(session: SessionInfo): void {
  $session.set(session);
  $status.set({
    ...$status.get(),
    model: session.model,
  });
}

export function pushMessage(message: {
  id?: string;
  role: string;
  content: string;
  timestamp?: number;
}): Message {
  const fullMessage: Message = {
    id: message.id || `msg_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
    role: message.role,
    content: message.content,
    timestamp: message.timestamp ?? Date.now(),
  };
  $messages.set([...$messages.get(), fullMessage]);
  return fullMessage;
}

export function clearMessages(): void {
  $messages.set([]);
}

export function startToolCall(toolCall: {
  callId: string;
  name: string;
  args?: any;
}): ToolCall {
  const newCall: ToolCall = {
    callId: toolCall.callId,
    name: toolCall.name,
    args: toolCall.args ?? {},
    status: 'running',
  };
  $activeToolCalls.set([...$activeToolCalls.get(), newCall]);
  return newCall;
}

export function updateToolCall(
  callId: string,
  update: Partial<ToolCall>
): void {
  const current = $activeToolCalls.get();
  $activeToolCalls.set(
    current.map((call) =>
      call.callId === callId ? { ...call, ...update } : call
    )
  );
}

export function clearToolCalls(): void {
  $activeToolCalls.set([]);
}

export function setApproval(approval: ApprovalRequest | null): void {
  $approval.set(approval);
}

export function setStatus(status: Partial<StatusInfo>): void {
  $status.set({
    ...$status.get(),
    ...status,
  });
}

export function resetStore(): void {
  $session.set(initialSession);
  $messages.set([]);
  $activeToolCalls.set([]);
  $approval.set(null);
  $status.set(initialStatus);
}
