#!/usr/bin/env node
/**
 * run_openclaw_session.mjs — Real OpenClaw Agent Session Driver
 *
 * Runs an autonomous agent turn-taking loop inside the Kuasar Sandbox:
 * 1. Connects to the agent LLM proxy via OPENAI_BASE_URL
 * 2. Receives tool-calling plans (bash / workspace inspection / testing / patching)
 * 3. Executes tools locally in /workspace/repo using Node.js child_process
 * 4. Measures and outputs high-resolution V8 heap and RSS memory telemetry
 */

import { execSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { performance } from 'node:perf_hooks';

const BASE_URL = process.env.OPENAI_BASE_URL || 'http://127.0.0.1:8088/v1';
const API_KEY = process.env.OPENAI_API_KEY || 'mock-key';
const REPO_DIR = process.env.REPO_DIR || '/workspace/repo';
const MODE = process.env.AGENT_MODE || 'full'; // 'full' (4 turns), 'tool_only' (1 turn), 'idle'

async function postChat(messages, tools) {
  const t0 = performance.now();
  const res = await fetch(`${BASE_URL}/chat/completions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${API_KEY}`,
    },
    body: JSON.stringify({
      model: 'openclaw-gpt4',
      messages,
      tools,
      tool_choice: 'auto',
    }),
  });
  if (!res.ok) {
    throw new Error(`LLM API returned ${res.status}: ${await res.text()}`);
  }
  const data = await res.json();
  const durationMs = performance.now() - t0;
  return { data, durationMs };
}

function executeTool(name, argsJson) {
  const t0 = performance.now();
  let stdout = '';
  let stderr = '';
  let exitCode = 0;

  try {
    const args = JSON.parse(argsJson);
    if (name === 'bash') {
      stdout = execSync(args.command, {
        cwd: REPO_DIR,
        encoding: 'utf8',
        shell: '/bin/bash',
        timeout: 30000,
        maxBuffer: 10 * 1024 * 1024,
      });
    } else {
      stdout = `Unknown tool: ${name}`;
    }
  } catch (err) {
    exitCode = err.status || 1;
    stdout = err.stdout ? err.stdout.toString() : '';
    stderr = err.stderr ? err.stderr.toString() : err.message;
  }

  const durationMs = performance.now() - t0;
  return { stdout, stderr, exitCode, durationMs };
}

async function runAgentSession() {
  const sessionStart = performance.now();
  const memInitial = process.memoryUsage();

  console.log(`[OpenClaw] Starting agent session (mode=${MODE}, repo=${REPO_DIR})`);
  console.log(`[OpenClaw] Initial V8 Memory: RSS=${(memInitial.rss / 1024 / 1024).toFixed(1)}MB HeapUsed=${(memInitial.heapUsed / 1024 / 1024).toFixed(1)}MB`);

  const tools = [
    {
      type: 'function',
      function: {
        name: 'bash',
        description: 'Execute shell and git commands in the workspace repository',
        parameters: {
          type: 'object',
          properties: {
            command: { type: 'string', description: 'The bash command to run' },
          },
          required: ['command'],
        },
      },
    },
  ];

  const messages = [
    {
      role: 'system',
      content: 'You are OpenClaw, an autonomous software engineering agent. Inspect the codebase, run tests, implement requested changes, and verify git diffs.',
    },
    {
      role: 'user',
      content: 'Inspect the calculator repository at /workspace/repo, execute the test suite, add a new power(a, b) function to src/calc.py, and verify with tests.',
    },
  ];

  let turn = 0;
  let maxTurns = MODE === 'tool_only' ? 1 : 4;
  let turnStats = [];
  let peakRss = memInitial.rss;

  while (turn < maxTurns) {
    turn++;
    console.log(`\n--- Turn ${turn} ---`);

    // 1. Model planning / tool generation
    const { data: resp, durationMs: llmLatency } = await postChat(messages, tools);
    const choice = resp.choices[0];
    const assistantMsg = choice.message;
    messages.push(assistantMsg);

    if (choice.finish_reason === 'stop' || !assistantMsg.tool_calls) {
      console.log(`[OpenClaw] Assistant: ${assistantMsg.content}`);
      break;
    }

    // 2. Dispatch and execute tool calls
    for (const toolCall of assistantMsg.tool_calls) {
      const toolName = toolCall.function.name;
      const toolArgs = toolCall.function.arguments;
      console.log(`[OpenClaw Tool Dispatch] -> ${toolName}(${toolArgs.trim()})`);

      const { stdout, stderr, exitCode, durationMs: toolLatency } = executeTool(toolName, toolArgs);
      console.log(`[OpenClaw Tool Result] (exit=${exitCode}, ${toolLatency.toFixed(1)}ms):`);
      if (stdout.trim()) console.log(stdout.trim().split('\n').map(l => '  ' + l).join('\n'));
      if (stderr.trim()) console.error(stderr.trim().split('\n').map(l => '  [err] ' + l).join('\n'));

      const toolOutput = exitCode === 0 ? stdout : `Error (exit ${exitCode}):\n${stderr}\n${stdout}`;
      messages.push({
        role: 'tool',
        tool_call_id: toolCall.id,
        content: toolOutput,
      });

      const currentMem = process.memoryUsage();
      if (currentMem.rss > peakRss) peakRss = currentMem.rss;

      turnStats.push({
        turn,
        tool: toolName,
        llm_latency_ms: llmLatency,
        tool_latency_ms: toolLatency,
        rss_bytes: currentMem.rss,
        heap_used_bytes: currentMem.heapUsed,
      });
    }
  }

  const sessionDurationMs = performance.now() - sessionStart;
  const memFinal = process.memoryUsage();

  const report = {
    verdict: 'PASS',
    mode: MODE,
    duration_ms: sessionDurationMs,
    turns_completed: turn,
    initial_rss_mib: (memInitial.rss / 1024 / 1024).toFixed(2),
    peak_rss_mib: (peakRss / 1024 / 1024).toFixed(2),
    final_rss_mib: (memFinal.rss / 1024 / 1024).toFixed(2),
    final_heap_used_mib: (memFinal.heapUsed / 1024 / 1024).toFixed(2),
    turns: turnStats,
  };

  console.log(`\n============================================================`);
  console.log(`[OpenClaw Session Complete] Verdict: PASS (${sessionDurationMs.toFixed(1)}ms)`);
  console.log(`  Peak RSS: ${report.peak_rss_mib} MiB | Final Heap: ${report.final_heap_used_mib} MiB`);
  console.log(`============================================================`);

  // Write machine-readable output to /tmp/openclaw-result.json
  writeFileSync('/tmp/openclaw-result.json', JSON.stringify(report, null, 2));
}

runAgentSession().catch(err => {
  console.error(`[OpenClaw Fatal Error]`, err);
  const failReport = { verdict: 'FAIL', error: err.message, stack: err.stack };
  writeFileSync('/tmp/openclaw-result.json', JSON.stringify(failReport, null, 2));
  process.exit(1);
});
