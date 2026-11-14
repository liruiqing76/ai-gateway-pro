// ============================================================
// Qoder 加密端点签名 sidecar — 逐行复用 dsh-our-free-model(EBONY-VINYL,
// MIT)渠道包 pack.js qoder-wasm.ts 的 JS glue 与 QoderEncryptedInfer。
//
// 用法(python 侧通过 stdin/stdout 行协议常驻调用):
//   node qoder_signer.mjs            服务循环, 每行一个 JSON 请求, 每行一个响应
//   请求: {"op":"ping"} | {"op":"auth","user":{...}} |
//         {"op":"infer","machineId":..,"user":{...},"metadata":{...},"host":..,
//          "ask":{modelKey,userText,...}}
//   响应: {"ok":true,"result":...} | {"ok":false,"error":"..."}
//
// 与插件的差异仅在传输层(文件读取 → stdio), glue/wasm 调用序列原样保留。
// ============================================================
import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WASM_PATH = resolve(dirname(fileURLToPath(import.meta.url)), "qoder-auth-wasm.wasm");
const COSY_VERSION = "1.1.49";
const IMPORT_MODULE = "./qoder_auth_wasm_bg.js";

// ---- glue: buildQoderInferPayload(照抄 pack.js L37037-37110) ----
export function buildQoderInferPayload(ask, requestId = randomUUID()) {
  const isReasoning = ask.isReasoning ?? false;
  const text = ask.userText;
  const parameters = {};
  if (ask.maxTokens !== undefined) parameters.max_tokens = ask.maxTokens;
  if (ask.reasoningEffort !== undefined) {
    parameters.reasoning_effort = ask.reasoningEffort;
    parameters.enable_thinking = ask.reasoningEffort !== "none";
  }
  if (ask.contextWindow !== undefined) parameters.context_length = ask.contextWindow;
  const messages = [];
  for (const m of ask.history ?? []) {
    messages.push({
      role: m.role,
      content: m.content,
      ...(m.tool_calls === undefined ? {} : { tool_calls: m.tool_calls }),
      ...(m.tool_call_id === undefined ? {} : { tool_call_id: m.tool_call_id }),
    });
  }
  if (messages.length === 0) messages.push({ role: "user", content: text });
  return {
    request_id: requestId,
    request_set_id: requestId,
    chat_record_id: requestId,
    session_id: randomUUID(),
    stream: true,
    chat_task: "FREE_INPUT",
    chat_context: {
      text,
      features: [],
      extra: {
        context: [],
        modelConfig: { key: ask.modelKey, is_reasoning: isReasoning },
        originalContent: text,
      },
      chatPrompt: "",
      imageUrls: null,
    },
    is_reply: true,
    is_retry: false,
    source: 1,
    version: "3",
    agent_id: "agent_common",
    task_id: "common",
    session_type: ask.sessionType ?? "qodercli",
    aliyun_user_type: "",
    model_config: {
      key: ask.modelKey,
      display_name: ask.displayName ?? "",
      model: "",
      format: ask.format ?? "openai",
      is_vl: ask.isVl ?? true,
      is_reasoning: isReasoning,
      api_key: "",
      url: "",
      source: ask.source ?? "system",
      max_input_tokens: ask.maxInputTokens ?? ask.contextWindow ?? 200000,
    },
    custom_model: null,
    system: ask.systemText ? [{ type: "text", text: ask.systemText }] : [],
    messages,
    tools: ask.tools ?? [],
    parameters,
    ...(ask.business === undefined ? {} : { business: ask.business }),
  };
}

// ---- glue: createGlue(照抄 pack.js L37116-37269) ----
let gluePromise = null;
async function getGlue() {
  gluePromise ??= createGlue();
  return gluePromise;
}
async function createGlue() {
  const bytes = readFileSync(WASM_PATH);
  const module = await WebAssembly.compile(bytes);
  let exports = {};
  let cachedHeap = null;
  let cachedView = null;
  const decoder = new TextDecoder("utf-8", { ignoreBOM: true, fatal: true });
  const encoder = new TextEncoder();
  const heap = () => {
    if (cachedHeap === null || cachedHeap.byteLength === 0) {
      cachedHeap = new Uint8Array(exports.memory.buffer);
    }
    return cachedHeap;
  };
  const view = () => {
    if (cachedView === null || cachedView.buffer !== exports.memory.buffer) {
      cachedView = new DataView(exports.memory.buffer);
    }
    return cachedView;
  };
  const readString = (ptr, len) => decoder.decode(heap().subarray(ptr >>> 0, (ptr >>> 0) + len));
  const objects = new Array(1024).fill(undefined);
  objects.push(undefined, null, true, false);
  let firstFree = objects.length;
  const heapObject = (index) => objects[index];
  const pushObject = (value) => {
    if (firstFree === objects.length) objects.push(objects.length + 1);
    const index = firstFree;
    firstFree = objects[index];
    objects[index] = value;
    return index;
  };
  const takeObject = (index) => {
    const value = heapObject(index);
    if (index >= 1028) {
      objects[index] = firstFree;
      firstFree = index;
    }
    return value;
  };
  let lastLength = 0;
  const writeString = (text) => {
    const encoded = encoder.encode(text);
    const ptr = exports.__wbindgen_export2(encoded.length, 1) >>> 0;
    heap().subarray(ptr, ptr + encoded.length).set(encoded);
    lastLength = encoded.length;
    return ptr;
  };
  const callString = (invoke) => {
    let ptr = 0;
    let len = 0;
    const stack = exports.__wbindgen_add_to_stack_pointer(-16);
    try {
      invoke(stack);
      const v = view();
      ptr = v.getInt32(stack + 0, true);
      len = v.getInt32(stack + 4, true);
      const isError = v.getInt32(stack + 12, true);
      if (isError) throw takeObject(v.getInt32(stack + 8, true));
      return readString(ptr, len);
    } finally {
      exports.__wbindgen_add_to_stack_pointer(16);
      if (ptr) exports.__wbindgen_export4(ptr, len, 1);
    }
  };
  const callPointer = (invoke) => {
    const stack = exports.__wbindgen_add_to_stack_pointer(-16);
    try {
      invoke(stack);
      const v = view();
      const ptr = v.getInt32(stack + 0, true);
      const isError = v.getInt32(stack + 8, true);
      if (isError) throw takeObject(v.getInt32(stack + 4, true));
      return ptr;
    } finally {
      exports.__wbindgen_add_to_stack_pointer(16);
    }
  };
  const guard = (fn, args) => {
    try {
      fn(...args);
    } catch (error) {
      exports.__wbindgen_export(pushObject(error));
    }
  };
  const imports = {
    [IMPORT_MODULE]: {
      __wbindgen_object_drop_ref: (a) => takeObject(a),
      __wbindgen_object_clone_ref: (a) => pushObject(heapObject(a)),
      __wbindgen_cast_0000000000000001: (a, e) => pushObject(heap().subarray(a >>> 0, (a >>> 0) + e)),
      __wbindgen_cast_0000000000000002: (a, e) => pushObject(readString(a, e)),
      __wbg_set_08463b1df38a7e29: (a, e, t) => pushObject(heapObject(a).set(heapObject(e), heapObject(t))),
      __wbg_getRandomValues_d49329ff89a07af1: (...a) => guard((x, y) => {
        globalThis.crypto.getRandomValues(heap().subarray(x >>> 0, (x >>> 0) + y));
      }, a),
      __wbg_getRandomValues_c44a50d8cfdaebeb: (...a) => guard((x, y) => {
        heapObject(x).getRandomValues(heapObject(y));
      }, a),
      __wbg_crypto_38df2bab126b63dc: (a) => pushObject(heapObject(a).crypto),
      __wbg_process_44c7a14e11e9f69e: (a) => pushObject(heapObject(a).process),
      __wbg_versions_276b2795b1c6a219: (a) => pushObject(heapObject(a).versions),
      __wbg_node_84ea875411254db1: (a) => pushObject(heapObject(a).node),
      __wbg_require_b4edbdcf3e2a1ef0: (...a) => guard(() => pushObject(module), a),
      __wbg_msCrypto_bd5a034af96bcba6: (a) => pushObject(heapObject(a).msCrypto),
      __wbg_randomFillSync_6c25eac9869eb53c: (...a) => guard((x, y) => {
        heapObject(x).randomFillSync(takeObject(y));
      }, a),
      __wbg_call_d578befcc3145dee: (...a) => guard((fn, self, arg) => {
        const target = heapObject(fn);
        pushObject(target.call(heapObject(self), heapObject(arg)));
      }, a),
      __wbg_new_with_length_9cedd08484b73942: (a) => pushObject(new Uint8Array(a >>> 0)),
      __wbg_length_0c32cb8543c8e4c8: (a) => heapObject(a).length,
      __wbg_prototypesetcall_3e05eb9545565046: (a, e, t) => {
        Uint8Array.prototype.set.call(heap().subarray(a >>> 0, (a >>> 0) + e), heapObject(t));
      },
      __wbg_subarray_0f98d3fb634508ad: (a, e, t) => pushObject(heapObject(a).subarray(e >>> 0, t >>> 0)),
      __wbg_new_99cabae501c0a8a0: () => pushObject(new Map()),
      __wbg_now_88621c9c9a4f3ffc: () => Date.now(),
      __wbg_static_accessor_GLOBAL_THIS_a1248013d790bf5f: () => pushObject(globalThis),
      __wbg_static_accessor_GLOBAL_f2e0f995a21329ff: () => pushObject(globalThis),
      __wbg_static_accessor_SELF_24f78b6d23f286ea: () => globalThis.self === undefined ? 0 : pushObject(globalThis.self),
      __wbg_static_accessor_WINDOW_59fd959c540fe405: () => globalThis.window === undefined ? 0 : pushObject(globalThis.window),
      __wbg___wbindgen_throw_81fc77679af83bc6: (p, l) => {
        throw new Error(readString(p, l));
      },
      __wbg_Error_2e59b1b37a9a34c3: (p, l) => pushObject(new Error(readString(p, l))),
      __wbg___wbindgen_is_object_40c5a80572e8f9d3: (id) => {
        const v = heapObject(id);
        return typeof v === "object" && v !== null;
      },
      __wbg___wbindgen_is_string_b29b5c5a8065ba1a: (id) => typeof heapObject(id) === "string",
      __wbg___wbindgen_is_function_49868bde5eb1e745: (id) => typeof heapObject(id) === "function",
      __wbg___wbindgen_is_undefined_c0cca72b82b86f4d: (id) => heapObject(id) === undefined,
    },
  };
  const instance = await WebAssembly.instantiate(module, imports);
  exports = instance.exports;
  return { exports, heap, view, readString, writeString, lastLength: () => lastLength, heapObject, pushObject, takeObject, callString, callPointer };
}

// ---- QoderEncryptedInfer(照抄 pack.js L37270-37376, 去掉 fs) ----
async function generateRuntimeAuthFields(user) {
  const g = await getGlue();
  const payload = JSON.stringify({
    uid: user.uid,
    security_oauth_token: user.securityOauthToken,
    organization_id: user.organizationId ?? "",
    organization_tags: user.organizationTags ?? [],
    data_policy_agreed: user.dataPolicyAgreed ?? false,
  });
  const raw = g.callString((stack) => {
    const a = g.writeString(payload);
    g.exports.generate_runtime_auth_fields(stack, a, g.lastLength());
  });
  return JSON.parse(raw);
}

class QoderEncryptedInfer {
  constructor(g, context, metadata, host) {
    this.g = g;
    this.context = context;
    this.metadata = metadata;
    this.host = host;
  }
  static async create(options) {
    const g = await getGlue();
    const fields = await generateRuntimeAuthFields(options.user);
    const version = options.clientVersion ?? COSY_VERSION;
    const userInfoJson = JSON.stringify({
      uid: options.user.uid,
      encrypt_user_info: fields.encrypt_user_info,
      key: fields.key,
      organization_id: options.user.organizationId ?? "",
      organization_tags: options.user.organizationTags ?? [],
      data_policy_agreed: options.user.dataPolicyAgreed ?? false,
    });
    const context = g.callPointer((stack) => {
      const machine = g.writeString(options.machineId);
      const machineLen = g.lastLength();
      const ver = g.writeString(version);
      const verLen = g.lastLength();
      const info = g.writeString(userInfoJson);
      const infoLen = g.lastLength();
      const meta = g.writeString(JSON.stringify(options.metadata));
      const metaLen = g.lastLength();
      g.exports.qodercontext_new(stack, machine, machineLen, ver, verLen, info, infoLen, meta, metaLen);
    });
    return { client: new QoderEncryptedInfer(g, context, options.metadata, options.host), fields };
  }
  prepareInfer(ask) {
    const g = this.g;
    const payload = buildQoderInferPayload(ask);
    const result = g.callPointer((stack) => {
      const host = g.writeString(this.host);
      const hostLen = g.lastLength();
      const body = g.writeString(JSON.stringify(payload));
      const bodyLen = g.lastLength();
      const key = g.writeString(ask.modelKey);
      const keyLen = g.lastLength();
      const source = g.writeString(ask.source ?? "system");
      const sourceLen = g.lastLength();
      g.exports.qodercontext_prepareInferRequest(stack, this.context, host, hostLen, body, bodyLen, key, keyLen, source, sourceLen);
    });
    const headerMap = g.takeObject(g.exports.requestresult_headers(result));
    const headers = {};
    if (headerMap instanceof Map) {
      for (const [k, v] of headerMap) headers[String(k)] = String(v);
    }
    const readResultString = (invoke) => {
      let out = "";
      const v = g.view();
      const stack = g.exports.__wbindgen_add_to_stack_pointer(-16);
      try {
        invoke(stack, result);
        const ptr = v.getInt32(stack + 0, true);
        const len = v.getInt32(stack + 4, true);
        out = ptr ? g.readString(ptr, len) : "";
      } finally {
        g.exports.__wbindgen_add_to_stack_pointer(16);
      }
      return out;
    };
    return {
      url: readResultString((stack, ptr) => g.exports.requestresult_url(stack, ptr)),
      headers,
      body: readResultString((stack, ptr) => g.exports.requestresult_body(stack, ptr)),
    };
  }
}

// ---- stdio 行协议服务 ----
const rl = (await import("node:readline")).createInterface({ input: process.stdin });
for await (const line of rl) {
  if (!line.trim()) continue;
  let resp;
  try {
    const req = JSON.parse(line);
    if (req.op === "ping") {
      resp = { ok: true, result: "pong" };
    } else if (req.op === "auth") {
      resp = { ok: true, result: await generateRuntimeAuthFields(req.user) };
    } else if (req.op === "infer") {
      const { client, fields } = await QoderEncryptedInfer.create({
        user: req.user, machineId: req.machineId,
        metadata: req.metadata, host: req.host,
      });
      const out = client.prepareInfer(req.ask);
      resp = { ok: true, result: { ...out, authFields: fields } };
    } else {
      resp = { ok: false, error: `unknown op: ${req.op}` };
    }
  } catch (e) {
    resp = { ok: false, error: String(e?.stack ?? e) };
  }
  process.stdout.write(JSON.stringify(resp) + "\n");
}
