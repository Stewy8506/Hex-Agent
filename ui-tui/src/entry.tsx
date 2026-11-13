import React, { useState } from 'react';
import { render, Box, Text } from 'ink';
import TextInput from 'ink-text-input';
import { useStore } from '@nanostores/react';
import {
  $session,
  $messages,
  $activeToolCalls,
  $approval,
  $status,
  pushMessage,
} from './domain/store.js';
import { GatewayClient } from './gatewayClient.js';

export function HexTuiApp() {
  const session = useStore($session);
  const messages = useStore($messages);
  const activeToolCalls = useStore($activeToolCalls);
  const approval = useStore($approval);
  const status = useStore($status);
  const [input, setInput] = useState('');

  return (
    <Box flexDirection="column" padding={1}>
      <Box borderStyle="round" borderColor="cyan" paddingX={1} justifyContent="space-between">
        <Text bold color="cyan">HEX Autonomous AI Workbench</Text>
        <Text dimColor>Model: {status.model} | Session: {session.sessionId || 'Ready'}</Text>
      </Box>

      <Box flexDirection="column" marginY={1}>
        {messages.length === 0 ? (
          <Text dimColor>Type a prompt to begin pair programming with HEX...</Text>
        ) : (
          messages.map((msg) => (
            <Box key={msg.id} flexDirection="column" marginY={0}>
              <Box flexDirection="row">
                <Text bold color={msg.role === 'user' ? 'green' : msg.role === 'system' ? 'magenta' : 'blue'}>
                  {msg.role === 'user' ? '❯ User: ' : msg.role === 'system' ? '⚠ System: ' : '✦ HEX: '}
                </Text>
                <Text>{msg.content}</Text>
              </Box>
            </Box>
          ))
        )}
      </Box>

      {status.thinking && (
        <Box marginY={0}>
          <Text color="yellow">⏳ Thinking & reasoning...</Text>
        </Box>
      )}

      {activeToolCalls.length > 0 && (
        <Box flexDirection="column" borderStyle="single" borderColor="yellow" paddingX={1} marginY={1}>
          <Text bold color="yellow">Tool Execution Journal:</Text>
          {activeToolCalls.map((tc) => (
            <Text key={tc.callId} dimColor>
              [{tc.status}] {tc.name} {JSON.stringify(tc.args)}
            </Text>
          ))}
        </Box>
      )}

      {approval && (
        <Box flexDirection="column" borderStyle="double" borderColor="red" paddingX={1} marginY={1}>
          <Text bold color="red">APPROVAL REQUIRED [R3/R4 Risk Action]:</Text>
          <Text>Tool: {approval.tool}</Text>
          {approval.diff && <Text dimColor>{approval.diff}</Text>}
        </Box>
      )}

      <Box marginTop={1} flexDirection="row">
        <Text bold color="cyan">hex&gt; </Text>
        <TextInput
          value={input}
          onChange={setInput}
          onSubmit={(value) => {
            const trimmed = value.trim();
            if (!trimmed) return;
            pushMessage({ role: 'user', content: trimmed });
            setInput('');
          }}
        />
      </Box>
    </Box>
  );
}

// Only mount directly when not imported in tests or headless mode
if (process.env.NODE_ENV !== 'test' && !process.env.HEX_NO_MOUNT && process.argv[1]?.endsWith('dist/index.js')) {
  render(<HexTuiApp />);
}

export default HexTuiApp;
