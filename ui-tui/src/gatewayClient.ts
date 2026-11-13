import { spawn, ChildProcess } from 'node:child_process';
import { Readable, Writable } from 'node:stream';
import * as readline from 'node:readline';
import {
  $session,
  $messages,
  $activeToolCalls,
  $approval,
  $status,
  setSession,
  pushMessage,
  startToolCall,
  updateToolCall,
  setStatus,
  SessionInfo,
  Message,
  ToolCall,
} from './domain/store.js';

export interface JSONRPCRequest {
  jsonrpc: '2.0';
  id: number | string;
  method: string;
  params?: any;
}

export interface JSONRPCResponse {
  jsonrpc: '2.0';
  id: number | string | null;
  result?: any;
  error?: {
    code: number;
    message: string;
    data?: any;
  };
}

export interface GatewayClientOptions {
  spawnCommand?: string;
  spawnArgs?: string[];
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  childProcess?: ChildProcess;
  inputStream?: Readable;
  outputStream?: Writable;
  requestTimeoutMs?: number;
}

export class GatewayClient {
  private childProcess: ChildProcess | null = null;
  private inputStream: Readable | null = null;
  private outputStream: Writable | null = null;
  private rl: readline.Interface | null = null;
  private nextId = 1;
  private pendingRequests = new Map<
    number | string,
    {
      resolve: (res: any) => void;
      reject: (err: any) => void;
      timer: NodeJS.Timeout;
    }
  >();
  private requestTimeoutMs: number;
  private isClosed = false;

  constructor(options: GatewayClientOptions = {}) {
    this.requestTimeoutMs = options.requestTimeoutMs ?? 60000;

    if (options.inputStream && options.outputStream) {
      this.inputStream = options.inputStream;
      this.outputStream = options.outputStream;
      this.setupReadline(this.inputStream);
    } else if (options.childProcess) {
      this.childProcess = options.childProcess;
      this.inputStream = this.childProcess.stdout;
      this.outputStream = this.childProcess.stdin;
      if (this.inputStream) {
        this.setupReadline(this.inputStream);
      }
    } else {
      const cmd = options.spawnCommand ?? (process.env.HEX_PYTHON || 'python3');
      const args = options.spawnArgs ?? ['-m', 'harness_core.cli', 'acp'];
      this.childProcess = spawn(cmd, args, {
        cwd: options.cwd ?? process.cwd(),
        env: { ...process.env, ...options.env },
        stdio: ['pipe', 'pipe', 'inherit'],
      });

      this.inputStream = this.childProcess.stdout;
      this.outputStream = this.childProcess.stdin;

      this.childProcess.on('error', (err) => {
        this.handleProcessError(err);
      });

      this.childProcess.on('exit', (code, signal) => {
        this.handleProcessExit(code, signal);
      });

      if (this.inputStream) {
        this.setupReadline(this.inputStream);
      }
    }
  }

  private setupReadline(input: Readable): void {
    this.rl = readline.createInterface({
      input,
      crlfDelay: Infinity,
    });

    this.rl.on('line', (line: string) => {
      this.handleIncomingLine(line);
    });
  }

  private handleIncomingLine(line: string): void {
    const trimmed = line.trim();
    if (!trimmed) return;

    try {
      const msg = JSON.parse(trimmed);
      this.handleIncomingMessage(msg);
    } catch {
      // Ignore unparseable non-JSON output (or Content-Length framing header lines)
    }
  }

  private handleIncomingMessage(msg: any): void {
    if (msg && typeof msg === 'object') {
      // JSON-RPC 2.0 Response
      if ('id' in msg && msg.id !== null && msg.id !== undefined) {
        const pending = this.pendingRequests.get(msg.id);
        if (pending) {
          clearTimeout(pending.timer);
          this.pendingRequests.delete(msg.id);

          if (msg.error) {
            pending.reject(
              new Error(
                `JSON-RPC Error [${msg.error.code}]: ${msg.error.message}`
              )
            );
          } else {
            pending.resolve(msg.result);
          }
        }
      }

      // Progress / Notification event routing
      if (msg.method) {
        this.handleNotification(msg.method, msg.params);
      }
    }
  }

  private handleNotification(method: string, params: any): void {
    if (method === 'session/message') {
      pushMessage({
        id: params?.id,
        role: params?.role || 'assistant',
        content: params?.content || '',
      });
    } else if (method === 'session/toolCall') {
      if (params?.status === 'running') {
        startToolCall({
          callId: params.callId,
          name: params.name,
          args: params.args,
        });
      } else if (params?.status === 'completed' || params?.status === 'error') {
        updateToolCall(params.callId, {
          status: params.status,
          output: params.output,
        });
      }
    } else if (method === 'session/approval') {
      $approval.set({
        id: params.id,
        tool: params.tool,
        args: params.args,
        diff: params.diff,
      });
    } else if (method === 'session/status') {
      setStatus({
        thinking: params.thinking,
        tokens: params.tokens,
        ready: params.ready,
      });
    }
  }

  private handleProcessError(err: Error): void {
    for (const [, pending] of this.pendingRequests) {
      clearTimeout(pending.timer);
      pending.reject(new Error(`Subprocess error: ${err.message}`));
    }
    this.pendingRequests.clear();
    setStatus({ ready: false, thinking: false });
  }

  private handleProcessExit(code: number | null, signal: NodeJS.Signals | null): void {
    this.isClosed = true;
    for (const [, pending] of this.pendingRequests) {
      clearTimeout(pending.timer);
      pending.reject(
        new Error(`Subprocess exited with code ${code} / signal ${signal}`)
      );
    }
    this.pendingRequests.clear();
    setStatus({ ready: false, thinking: false });
  }

  public request<T = any>(method: string, params: any = {}): Promise<T> {
    if (this.isClosed) {
      return Promise.reject(new Error('GatewayClient is closed'));
    }

    const id = this.nextId++;
    const req: JSONRPCRequest = {
      jsonrpc: '2.0',
      id,
      method,
      params,
    };

    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingRequests.delete(id);
        reject(
          new Error(
            `Request ${method} (id=${id}) timed out after ${this.requestTimeoutMs}ms`
          )
        );
      }, this.requestTimeoutMs);

      this.pendingRequests.set(id, { resolve, reject, timer });

      const raw = JSON.stringify(req) + '\n';
      if (!this.outputStream || !this.outputStream.write(raw)) {
        // Output stream failed
        clearTimeout(timer);
        this.pendingRequests.delete(id);
        reject(new Error('Failed to write to gateway output stream'));
      }
    });
  }

  public async initialize(): Promise<{
    protocolVersion: string;
    capabilities: any;
    serverInfo: { name: string; version: string };
  }> {
    const res = await this.request('initialize', {
      clientInfo: {
        name: 'HEX-TUI',
        version: '0.1.0',
      },
      capabilities: {
        streaming: true,
      },
    });
    setStatus({ ready: true });
    return res;
  }

  public async newSession(
    cwd?: string,
    model?: string
  ): Promise<SessionInfo> {
    const res = await this.request('session/new', {
      cwd: cwd ?? process.cwd(),
      model: model ?? 'default',
    });
    const session: SessionInfo = {
      sessionId: res.sessionId,
      model: res.model || model || 'default',
      workspaceRoot: res.cwd || cwd || process.cwd(),
    };
    setSession(session);
    return session;
  }

  public async loadSession(sessionId: string): Promise<SessionInfo> {
    const res = await this.request('session/load', { sessionId });
    const session: SessionInfo = {
      sessionId: res.sessionId,
      model: res.model || 'default',
      workspaceRoot: res.cwd || process.cwd(),
    };
    setSession(session);
    return session;
  }

  public async sendPrompt(
    sessionId: string,
    prompt: string
  ): Promise<{
    sessionId: string;
    status: string;
    content: Array<{ type: string; text: string }>;
    taskId?: string;
  }> {
    // Record user message
    pushMessage({
      role: 'user',
      content: prompt,
    });
    setStatus({ thinking: true });

    try {
      const res = await this.request('session/prompt', {
        sessionId,
        prompt,
      });

      // Extract assistant response content
      const textParts = (res.content || [])
        .filter((c: any) => c.type === 'text')
        .map((c: any) => c.text)
        .join('\n');

      if (textParts) {
        pushMessage({
          role: 'assistant',
          content: textParts,
        });
      }

      setStatus({ thinking: false });
      return res;
    } catch (err) {
      setStatus({ thinking: false });
      pushMessage({
        role: 'system',
        content: `Error: ${err instanceof Error ? err.message : String(err)}`,
      });
      throw err;
    }
  }

  public async cancelSession(
    sessionId: string
  ): Promise<{ sessionId: string; cancelled: boolean }> {
    const res = await this.request('session/cancel', { sessionId });
    setStatus({ thinking: false });
    return res;
  }

  public async listTools(): Promise<{ tools: any[] }> {
    return await this.request('tools/list', {});
  }

  public async callTool(
    name: string,
    args: Record<string, any> = {}
  ): Promise<{ content: Array<{ type: string; text: string }> }> {
    const callId = `call_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    startToolCall({
      callId,
      name,
      args,
    });

    try {
      const res = await this.request('tools/call', {
        name,
        arguments: args,
      });
      updateToolCall(callId, {
        status: 'completed',
        output: res.content,
      });
      return res;
    } catch (err) {
      updateToolCall(callId, {
        status: 'error',
        output: err instanceof Error ? err.message : String(err),
      });
      throw err;
    }
  }

  public async ping(): Promise<{ pong: boolean }> {
    return await this.request('ping', {});
  }

  public close(): void {
    this.isClosed = true;
    if (this.rl) {
      this.rl.close();
      this.rl = null;
    }
    if (this.childProcess) {
      this.childProcess.kill();
      this.childProcess = null;
    }
    for (const [, pending] of this.pendingRequests) {
      clearTimeout(pending.timer);
      pending.reject(new Error('GatewayClient closed'));
    }
    this.pendingRequests.clear();
    setStatus({ ready: false, thinking: false });
  }
}
