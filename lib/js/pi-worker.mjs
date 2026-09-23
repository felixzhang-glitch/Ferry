#!/usr/bin/env node
// Ferry owns process lifetime; pi owns prompts, tools, extensions and transcripts.
import { createReadStream, existsSync, lstatSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { readFile, readdir, stat } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SUPPORTED_VERSION = '0.84.2';
const MAX_LINE_BYTES = 32 * 1024 * 1024;
const MAX_INPUT_BYTES = 8 * 1024 * 1024;

export function parseRunArgs(args) {
  if (!Array.isArray(args) || args.length < 3 || args.some(arg => typeof arg !== 'string' || arg.includes('\0'))) {
    throw new Error('Expected CLI argument strings and a final prompt');
  }
  // The last argument is ALWAYS the user prompt, including -flags and @text.
  const prompt = args.at(-1);
  const parsed = { prompt, files: [], appendSystemPrompt: undefined, approve: false };
  const flags = new Map([
    ['--mode', 'mode'], ['--session-id', 'sessionId'], ['--model', 'model'],
    ['--thinking', 'thinking'], ['--tools', 'tools'],
  ]);
  const seen = new Set();
  for (let i = 0; i < args.length - 1; i++) {
    const arg = args[i];
    if (arg.startsWith('@') && isAbsolute(arg.slice(1))) {
      parsed.files.push(arg.slice(1));
      continue;
    }
    if (arg === '--approve') {
      if (parsed.approve) throw new Error('Duplicate --approve');
      parsed.approve = true;
      continue;
    }
    if (arg !== '--append-system-prompt' && !flags.has(arg)) {
      // Do not echo unknown arguments: they may contain private prompt/key text.
      throw new Error('Unsupported worker CLI argument');
    }
    if (++i >= args.length - 1) throw new Error('Missing worker CLI flag value');
    const value = args[i];
    if (arg === '--append-system-prompt') {
      (parsed.appendSystemPrompt ??= []).push(value);
    } else {
      if (seen.has(arg)) throw new Error(`Duplicate ${arg}`);
      seen.add(arg);
      parsed[flags.get(arg)] = value;
    }
  }
  if (parsed.mode !== 'json') throw new Error('Worker requires --mode json');
  // Like CLI without --session-id, unkeyed calls get a new native session.
  parsed.sessionId ??= randomUUID();
  if (!parsed.sessionId || parsed.sessionId.length > 200 || !/^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/.test(parsed.sessionId)) {
    throw new Error('Worker requires a valid exact --session-id');
  }
  if (parsed.thinking !== undefined && !['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].includes(parsed.thinking)) {
    throw new Error('Invalid thinking level');
  }
  if (parsed.model === '') throw new Error('Empty model');
  if (parsed.tools !== undefined) parsed.tools = parsed.tools.split(',').map(s => s.trim()).filter(Boolean);
  return parsed;
}

// Both input and validation streams are bounded, unlike readline's line queue.
export async function* jsonLines(stream, limit = MAX_INPUT_BYTES) {
  stream.setEncoding('utf8');
  let pending = '';
  for await (const chunk of stream) {
    pending += chunk;
    let newline;
    while ((newline = pending.indexOf('\n')) !== -1) {
      const line = pending.slice(0, newline);
      pending = pending.slice(newline + 1);
      if (Buffer.byteLength(line) > limit) throw new Error('JSONL line exceeds size limit');
      if (line.trim()) yield line;
    }
    if (Buffer.byteLength(pending) > limit) throw new Error('JSONL line exceeds size limit');
  }
  if (pending.trim()) yield pending;
}

export function createProtocolWriter(write, { maxLineBytes = MAX_LINE_BYTES, maxQueuedBytes = 64 * 1024 * 1024, onError = () => {} } = {}) {
  let tail = Promise.resolve();
  let queuedBytes = 0;
  let failure;
  return {
    send(event) {
      if (failure) throw failure;
      const line = `${JSON.stringify(event)}\n`;
      const bytes = Buffer.byteLength(line);
      if (bytes > maxLineBytes || queuedBytes + bytes > maxQueuedBytes) {
        throw new Error('Worker protocol output exceeds size limit');
      }
      queuedBytes += bytes;
      tail = tail.then(() => new Promise((accept, reject) => {
        write(line, error => error ? reject(error) : accept());
      })).finally(() => { queuedBytes -= bytes; });
      void tail.catch(error => {
        if (!failure) { failure = error; onError(error); }
      });
    },
    async flush() {
      let current;
      do { current = tail; await current; } while (current !== tail);
    },
  };
}

export async function loadSdk(modulePath) {
  if (!modulePath || !isAbsolute(modulePath) || !modulePath.endsWith('/dist/index.js')) {
    throw new Error('Expected an absolute pi dist/index.js module path');
  }
  const sdk = await import(pathToFileURL(modulePath).href);
  const metadata = JSON.parse(await readFile(join(dirname(modulePath), '..', 'package.json'), 'utf8'));
  if (metadata.version !== SUPPORTED_VERSION || sdk.VERSION !== SUPPORTED_VERSION) {
    throw new Error(`Worker requires pi ${SUPPORTED_VERSION}`);
  }
  const internal = async path => import(pathToFileURL(join(dirname(modulePath), path)).href);
  const modules = await Promise.all([
    internal('core/session-manager.js'), internal('core/project-trust.js'),
    internal('cli/project-trust.js'), internal('utils/image-process.js'),
    internal('utils/mime.js'), internal('core/tools/path-utils.js'),
    internal('modes/json-event.js'), internal('core/extensions/loader.js'),
    internal('core/resolve-config-value.js'), internal('utils/shell.js'),
    internal('core/http-dispatcher.js'),
  ]);
  const loaded = Object.assign({}, sdk, ...modules);
  // Node's bundled undici (or another installed copy) may use a different global slot.
  const { getGlobalDispatcher } = createRequire(modulePath)('undici');
  loaded.initializeHttp = createHttpInitializer(loaded, getGlobalDispatcher);
  return loaded;
}

export function createHttpInitializer(sdk, getGlobalDispatcher) {
  const injected = new Map();
  let current;
  return async (settingsManager, deferIdleTimeout = false) => {
    // Undo only our last injection. Inherited values, including empty strings and
    // lower-case overrides, must retain exactly the native CLI's precedence.
    for (const [key, value] of injected) {
      if (process.env[key] === value) delete process.env[key];
    }
    injected.clear();
    const missing = ['HTTP_PROXY', 'HTTPS_PROXY'].filter(key => process.env[key] === undefined);
    sdk.applyHttpProxySettings(settingsManager.getGlobalSettings().httpProxy);
    for (const key of missing) {
      if (process.env[key] !== undefined) injected.set(key, process.env[key]);
    }
    // Project trust is resolved during service loading. Keep the previous timeout
    // during that bootstrap pass instead of reverting to the default every turn.
    const timeoutMs = deferIdleTimeout
      ? current?.timeoutMs ?? sdk.DEFAULT_HTTP_IDLE_TIMEOUT_MS
      : settingsManager.getHttpIdleTimeoutMs();
    const httpProxy = (process.env.http_proxy ?? process.env.HTTP_PROXY) || '';
    const httpsProxy = (process.env.https_proxy ?? process.env.HTTPS_PROXY) || httpProxy;
    const config = JSON.stringify([timeoutMs, httpProxy, httpsProxy, process.env.no_proxy ?? process.env.NO_PROXY ?? '']);
    const previous = getGlobalDispatcher();
    if (current?.config === config && current.dispatcher === previous && !previous.closed && !previous.destroyed) return;
    sdk.configureHttpDispatcher(timeoutMs);
    const dispatcher = getGlobalDispatcher();
    current = { config, timeoutMs, dispatcher };
    if (previous && previous !== dispatcher) {
      // No active turn owns the old dispatcher. Destroy rather than waiting for
      // idle keep-alive sockets or an extension's unfinished response to drain.
      let timer;
      try {
        await Promise.race([
          previous.destroy(),
          new Promise(resolve => { timer = setTimeout(resolve, 1000); }),
        ]);
      } finally { clearTimeout(timer); }
    }
  };
}

export async function processAttachments(files, sdk, autoResizeImages) {
  let text = '';
  const images = [];
  for (const file of files) {
    const path = resolve(sdk.resolveReadPath(file, process.cwd()));
    const stats = await stat(path); // Missing/unreadable files throw, never process.exit.
    if (!stats.isFile()) throw new Error('Attachment is not a regular file');
    if (!stats.size) continue;
    const mimeType = await sdk.detectSupportedImageMimeTypeFromFile(path);
    if (!mimeType) {
      text += `<file name="${path}">\n${await readFile(path, 'utf8')}\n</file>\n`;
      continue;
    }
    const processed = await sdk.processImage(await readFile(path), mimeType, { autoResizeImages });
    if (!processed.ok) {
      text += `<file name="${path}">${processed.message}</file>\n`;
    } else {
      images.push({ type: 'image', mimeType: processed.mimeType, data: processed.data });
      text += `<file name="${path}">${processed.hints.join('\n')}</file>\n`;
    }
  }
  return { text, images };
}

async function validateSession(path, id, cwd, version) {
  if (!lstatSync(path).isFile()) throw new Error('Session is not a regular file');
  let header;
  for await (const line of jsonLines(createReadStream(path), MAX_LINE_BYTES)) {
    let entry;
    try { entry = JSON.parse(line); } catch { throw new Error('Corrupt session JSONL'); }
    if (!entry || typeof entry.type !== 'string') throw new Error('Corrupt session entry');
    if (!header) {
      header = entry;
      if (Buffer.byteLength(line) > 1024 * 1024 || header.type !== 'session' || header.id !== id ||
          typeof header.cwd !== 'string' || resolve(header.cwd) !== cwd ||
          !Number.isInteger(header.version) || header.version < 1 || header.version > version) {
        throw new Error('Session header does not match requested ID/cwd/version');
      }
    } else if (entry.type === 'session' || (entry.type === 'message' && (!entry.message || typeof entry.message.role !== 'string'))) {
      throw new Error('Corrupt session entry');
    }
  }
  if (!header) throw new Error('Empty existing session');
}

export async function openExactSession(sdk, cwd, agentDir, id, knownFiles = new Map()) {
  const directory = sdk.getDefaultSessionDir(cwd, agentDir);
  // Never list/parse every chat's history. Only the selected transcript is read.
  const matches = (await readdir(directory)).filter(name => {
    const separator = name.indexOf('_'); // Native timestamps contain no underscores.
    return separator >= 0 && name.slice(separator + 1) === `${id}.jsonl`;
  });
  if (matches.length > 1) throw new Error('Multiple transcripts for exact session ID');
  const remembered = knownFiles.get(id);
  if (!matches.length) {
    if (remembered) throw new Error('Previously persisted session is missing');
    return { manager: sdk.SessionManager.create(cwd, directory, { id }), resumed: false };
  }
  const path = join(directory, matches[0]);
  if (remembered && remembered !== path) throw new Error('Previously persisted session was replaced');
  await validateSession(path, id, cwd, sdk.CURRENT_SESSION_VERSION);
  // The SDK initializes empty/missing files; explicitly exclude those cases.
  if (!lstatSync(path).isFile() || !lstatSync(path).size) throw new Error('Existing session disappeared');
  const manager = sdk.SessionManager.open(path, directory);
  if (manager.getSessionId() !== id || manager.getCwd() !== cwd) throw new Error('Session identity changed while opening');
  return { manager, resumed: true };
}

export async function runTurn(request, sdk, writer, knownFiles = new Map()) {
  const started = performance.now();
  const requestId = typeof request?.id === 'string' ? request.id : null;
  const emit = event => writer.send({ ...event, requestId });
  let returncode = 0;
  const fail = error => {
    returncode = 1;
    emit({ type: 'extension_error', message: error instanceof Error ? error.message : String(error) });
  };
  let runtime;
  let manager;
  let unsubscribe;
  let unsubscribeBackpressure;
  let lastAssistant;
  const settings = new Set();
  try {
    if (request?.type !== 'run' || !requestId || requestId.length > 200) throw new Error('Invalid worker run request');
    const parsed = parseRunArgs(request.args);
    const cwd = process.cwd();
    const agentDir = sdk.getAgentDir();
    const opened = await openExactSession(sdk, cwd, agentDir, parsed.sessionId, knownFiles);
    manager = opened.manager;
    emit(manager.getHeader());
    // These are SDK process-level caches, not loader-instance caches.
    sdk.clearExtensionCache();
    sdk.clearConfigValueCache();
    const createRuntime = async target => {
      const targetCwd = target.cwd;
      const trustStore = new sdk.ProjectTrustStore(target.agentDir);
      const needsTrust = !parsed.approve && sdk.hasTrustRequiringProjectResources(targetCwd);
      const settingsManager = sdk.SettingsManager.create(targetCwd, target.agentDir, { projectTrusted: !needsTrust });
      settings.add(settingsManager);
      await sdk.initializeHttp(settingsManager, needsTrust);
      const services = await sdk.createAgentSessionServices({
        cwd: targetCwd, agentDir: target.agentDir, settingsManager,
        resourceLoaderOptions: { appendSystemPrompt: parsed.appendSystemPrompt },
        resourceLoaderReloadOptions: needsTrust ? {
          resolveProjectTrust: async ({ extensionsResult }) => sdk.resolveProjectTrusted({
            cwd: targetCwd, trustStore, extensionsResult,
            defaultProjectTrust: settingsManager.getDefaultProjectTrust(),
            projectTrustContext: target.projectTrustContext ?? sdk.createProjectTrustContext({
              cwd: targetCwd, mode: 'json', settingsManager, hasUI: false,
            }),
            onExtensionError: fail,
          }),
        } : undefined,
      });
      await sdk.initializeHttp(settingsManager);
      for (const diagnostic of services.diagnostics) if (diagnostic.type === 'error') fail(diagnostic.message);
      for (const error of services.resourceLoader.getExtensions().errors) fail(error.error);
      const resolved = sdk.resolveCliModel({ cliModel: parsed.model, cliThinking: parsed.thinking, modelRuntime: services.modelRuntime });
      if (resolved.error) throw new Error(resolved.error);
      let model = resolved.model;
      let thinkingLevel = parsed.thinking ?? resolved.thinkingLevel;
      const patterns = settingsManager.getEnabledModels();
      const { scopedModels } = patterns?.length
        ? await sdk.resolveModelScopeWithDiagnostics(patterns, services.modelRuntime)
        : { scopedModels: [] };
      if (!model && scopedModels.length && !target.sessionManager.buildSessionContext().messages.length) {
        const selected = scopedModels.find(item => item.model.provider === settingsManager.getDefaultProvider() && item.model.id === settingsManager.getDefaultModel()) ?? scopedModels[0];
        model = selected.model;
        thinkingLevel ??= selected.thinkingLevel;
      }
      const created = await sdk.createAgentSessionFromServices({
        services, sessionManager: target.sessionManager, sessionStartEvent: target.sessionStartEvent,
        model, thinkingLevel, scopedModels, tools: parsed.tools,
      });
      if (created.session.model && (parsed.thinking !== undefined || resolved.thinkingLevel !== undefined)) {
        created.session.setThinkingLevel(created.session.thinkingLevel);
      }
      return { ...created, services, diagnostics: services.diagnostics };
    };
    runtime = await sdk.createAgentSessionRuntime(createRuntime, { cwd, agentDir, sessionManager: manager });
    const rebind = async () => {
      unsubscribe?.();
      unsubscribeBackpressure?.();
      const session = runtime.session;
      // Attach before startup, so extension-generated events cannot be lost.
      unsubscribe = session.subscribe(event => {
        if (event.type === 'message_end' && event.message.role === 'assistant') lastAssistant = event.message;
        emit(sdk.toJsonEvent(event));
      });
      unsubscribeBackpressure = session.agent.subscribe(() => writer.flush());
      await session.bindExtensions({
        mode: 'json',
        onError: error => fail(error.error),
        commandContextActions: {
          waitForIdle: () => session.waitForIdle(),
          newSession: options => runtime.newSession(options),
          fork: async (id, options) => ({ cancelled: (await runtime.fork(id, options)).cancelled }),
          navigateTree: async (id, options) => ({ cancelled: (await session.navigateTree(id, options)).cancelled }),
          switchSession: (path, options) => runtime.switchSession(path, options),
          reload: () => session.reload(),
        },
      });
    };
    runtime.setRebindSession(rebind);
    await rebind();
    const attachments = await processAttachments(parsed.files, sdk, runtime.services.settingsManager.getImageAutoResize());
    emit({ type: 'ferry_turn_ready', pid: process.pid, setup_ms: Math.round(performance.now() - started), resumed: opened.resumed });
    await writer.flush();
    await runtime.session.prompt(attachments.text + parsed.prompt, { images: attachments.images.length ? attachments.images : undefined });
    // Extension commands, auto-retry and compaction may outlive agent_end.
    await runtime.session.waitForIdle();
    if (lastAssistant && ['error', 'aborted'].includes(lastAssistant.stopReason)) {
      fail(lastAssistant.errorMessage || `Request ${lastAssistant.stopReason}`);
    }
  } catch (error) {
    fail(error);
  } finally {
    if (runtime) {
      try { await runtime.session.abort(); await runtime.dispose(); } catch (error) { fail(error); }
      finally { runtime.session.dispose(); }
    }
    unsubscribe?.();
    unsubscribeBackpressure?.();
    for (const item of settings) {
      try {
        await item.flush();
        for (const error of item.drainErrors()) fail(error.error ?? 'pi settings persistence failed');
      } catch (error) { fail(error); }
    }
    sdk.killTrackedDetachedChildren();
    if (manager && existsSync(manager.getSessionFile())) {
      // A bounded path-only cache detects deletion between leases, never retains sessions.
      if (knownFiles.size >= 1024) knownFiles.delete(knownFiles.keys().next().value);
      knownFiles.set(manager.getSessionId(), manager.getSessionFile());
    }
  }
  emit({ type: 'ferry_done', returncode });
  await writer.flush();
}

async function main() {
  const exit = process.exit.bind(process);
  const rawWrite = process.stdout.write.bind(process.stdout);
  // Guard libraries and extensions too, not only console.log/info.
  process.stdout.write = process.stderr.write.bind(process.stderr);
  console.log = console.info = console.error.bind(console);
  process.exit = () => { throw new Error('process.exit is not allowed inside the pi worker'); };
  const fatal = () => exit(1); // No argument, payload or credential logging.
  process.stdout.on('error', fatal);
  const writer = createProtocolWriter(rawWrite, { onError: fatal });
  let sdk;
  try {
    sdk = await loadSdk(process.argv[2]);
    for (const signal of ['SIGTERM', 'SIGHUP']) {
      process.on(signal, () => { sdk.killTrackedDetachedChildren(); exit(signal === 'SIGTERM' ? 143 : 129); });
    }
    writer.send({ type: 'ferry_ready', protocol: 1, pid: process.pid, version: sdk.VERSION });
    await writer.flush();
    const knownFiles = new Map();
    for await (const line of jsonLines(process.stdin)) {
      let request;
      try { request = JSON.parse(line); } catch { throw new Error('Invalid worker JSONL'); }
      await runTurn(request, sdk, writer, knownFiles);
    }
    await writer.flush();
    sdk.killTrackedDetachedChildren();
    exit(0);
  } catch (error) {
    console.error(error instanceof Error ? error.message : 'pi worker failed');
    sdk?.killTrackedDetachedChildren();
    exit(1);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
