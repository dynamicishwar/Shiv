// app.js — Shiv's UI: chat + voice controls + pluggable avatar adapters.
// Talks to the Python backend over ws://127.0.0.1:<port>/ws.

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const LIVEAVATAR_SDK = "/ui/vendor/liveavatar-sdk.js";  // @heygen/liveavatar-web-sdk 0.0.19, bundled locally

const $ = (id) => document.getElementById(id);
const els = {
  conn: $("connDot"), name: $("assistantName"), provider: $("providerSelect"), avatarSel: $("avatarSelect"),
  clear: $("clearBtn"), stage: $("stage"), orb: $("orb"), video: $("avatarVideo"), pill: $("statePill"),
  hud: $("hud"), avatarToggle: $("avatarToggle"), chat: $("chat"), confirm: $("confirmCard"),
  confirmText: $("confirmText"), approve: $("approveBtn"), always: $("alwaysBtn"), deny: $("denyBtn"),
  mic: $("micBtn"), input: $("input"), send: $("sendBtn"), stop: $("stopBtn"), soundBtn: $("soundBtn"), audio: $("avatarAudio"),
};

const state = {
  ws: null, server: {}, thinking: false, listening: "idle", speaking: false,
  bubble: null, turnHadText: false, confirmQueue: [], avatarPref: "auto",
};

// ---------------------------------------------------------------- websocket
function send(obj) { if (state.ws?.readyState === 1) state.ws.send(JSON.stringify(obj)); }

// Mirror console output into D:\Shiv\logs\shiv_avatar.log (via the backend) for debugging.
for (const level of ["log", "info", "warn", "error"]) {
  const orig = console[level].bind(console);
  console[level] = (...args) => {
    orig(...args);
    try {
      const msg = args.map((a) => (a instanceof Error ? a.message : typeof a === "object" ? JSON.stringify(a)?.slice(0, 300) : String(a))).join(" ");
      send({ type: "log", level, msg: msg.slice(0, 1000) });
    } catch {}
  };
}
window.addEventListener("error", (e) => console.error("uncaught:", e.message));
window.addEventListener("unhandledrejection", (e) => console.error("unhandled rejection:", e.reason?.message || e.reason));

function videoState() {
  const v = els.video, src = v.srcObject;
  const tracks = src?.getTracks?.().map((t) => `${t.kind}:${t.readyState}${t.muted ? "(muted)" : ""}`).join(",");
  return `paused=${v.paused} muted=${v.muted} vol=${v.volume} ready=${v.readyState} size=${v.videoWidth}x${v.videoHeight} tracks=[${tracks || "none"}]`;
}

// The avatar's audio is played through its own <audio> element (more reliable
// than the <video> element's audio), and browsers may need a click to unlock it.
function wireAudio() {
  const ms = els.video.srcObject;
  const tracks = ms?.getAudioTracks?.() || [];
  if (!tracks.length) return console.warn("liveavatar: no audio track to play");
  els.audio.srcObject = new MediaStream(tracks);
  els.video.muted = true;  // avoid double audio; sound comes from els.audio
}

async function ensurePlaying() {
  const v = els.video, a = els.audio;
  if (!v.srcObject && !v.src) return;
  try { await avatar.live.session?.room?.startAudio?.(); } catch {}
  try {
    await v.play();
    if (a.srcObject) { a.muted = false; a.volume = 1; await a.play(); }
    els.soundBtn.hidden = true;
  } catch (e) {
    console.warn("avatar playback blocked:", e.name, e.message);
    els.soundBtn.hidden = false;
  }
}
function audioState() {
  const a = els.audio, room = avatar.live.session?.room;
  return `audioEl paused=${a.paused} muted=${a.muted} vol=${a.volume} tracks=${a.srcObject?.getAudioTracks().length || 0} canPlaybackAudio=${room?.canPlaybackAudio}`;
}
document.addEventListener("pointerdown", () => { if (els.video.srcObject) ensurePlaying(); }, true);

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(TOKEN)}`);
  state.ws = ws;
  ws.onopen = () => { els.conn.className = "dot on"; refreshPill(); };
  ws.onclose = () => { els.conn.className = "dot off"; setPill("Reconnecting…"); setTimeout(connect, 1500); };
  ws.onmessage = (e) => { try { onEvent(JSON.parse(e.data)); } catch (err) { console.error(err); } };
}

function onEvent(ev) {
  switch (ev.type) {
    case "state": applyState(ev); break;
    case "user_message": addUser(ev.text, ev.source); break;
    case "thinking": state.thinking = ev.on; toggleBusy(ev.on); refreshPill(); break;
    case "assistant_delta": appendDelta(ev.text); break;
    case "assistant_break": state.bubble = null; break;
    case "assistant_done": finishTurn(ev); break;
    case "tool_start": state.bubble = null; addTool(ev); break;
    case "tool_result": finishTool(ev); break;
    case "confirm_request": state.confirmQueue.push(ev); showConfirm(); break;
    case "confirm_closed": state.confirmQueue = state.confirmQueue.filter((c) => c.id !== ev.id); showConfirm(); break;
    case "listening": state.listening = ev.state; pttOn = ev.state === "recording"; els.mic.classList.toggle("rec", pttOn); refreshPill(); break;
    case "tts_state": setSpeaking(ev.speaking); break;
    case "interrupt": avatar.interrupt(); break;
    case "shutdown": if (avatar.live.ready) avatar.live.stop(); break;
    case "metrics": els.hud.textContent = `CPU ${Math.round(ev.cpu)}%  RAM ${Math.round(ev.ram)}%`; break;
    case "notice": addNotice(ev.text); break;
  }
}

function applyState(s) {
  state.server = s;
  els.name.textContent = s.assistant_name || "Shiv";
  document.title = `${s.assistant_name || "Shiv"} Assistant`;  // shell finds the window by this title
  els.input.placeholder = `Ask or tell ${s.assistant_name || "Shiv"} to do something…`;
  els.provider.value = s.llm_provider;
  const liveOk = s.avatar_provider && s.avatar_provider !== "local";
  els.avatarSel.querySelector('option[value="auto"]').disabled = !liveOk;
  if (!liveOk) { state.avatarPref = "local"; els.avatarSel.value = "local"; }
  els.avatarToggle.hidden = !liveOk || state.avatarPref !== "auto";
  document.querySelectorAll(".listen-modes button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === s.listening_mode);
    b.disabled = !s.stt_available && b.dataset.mode !== "off";
  });
  els.mic.disabled = !s.stt_available;
  if (!s.stt_available) els.mic.title = "Voice input not installed (see AVATAR_README.md)";
  refreshPill();
}

// ---------------------------------------------------------------- chat rendering
function esc(s) { return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

function md(text) {
  const parts = text.split(/```(\w*)\n?([\s\S]*?)```/g);
  let html = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 3 === 0) {
      html += esc(parts[i])
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2">$1</a>')
        .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2">$2</a>');
    } else if (i % 3 === 2) {
      html += `<pre><code>${esc(parts[i])}</code></pre>`;
    }
  }
  return html;
}

function scrollDown() { els.chat.scrollTop = els.chat.scrollHeight; }

function addUser(text, source) {
  const d = document.createElement("div");
  d.className = "msg user";
  d.innerHTML = (source === "voice" ? '<span class="src">🎙</span>' : "") + esc(text);
  els.chat.appendChild(d);
  state.bubble = null; state.turnHadText = false;
  scrollDown();
}

function appendDelta(text) {
  if (!state.bubble) {
    state.bubble = document.createElement("div");
    state.bubble.className = "msg assistant";
    state.bubble.dataset.raw = "";
    els.chat.appendChild(state.bubble);
  }
  state.bubble.dataset.raw += text;
  state.bubble.innerHTML = md(state.bubble.dataset.raw);
  state.turnHadText = true;
  scrollDown();
}

function finishTurn(ev) {
  if (!state.turnHadText && ev.text) appendDelta(ev.text);
  state.bubble = null; state.turnHadText = false;
  if (ev.speech) speak(ev.speech);
}

function addNotice(text) {
  const d = document.createElement("div");
  d.className = "notice"; d.textContent = text;
  els.chat.appendChild(d); scrollDown();
}

let lastTool = null;
function addTool(ev) {
  const d = document.createElement("details");
  d.className = "tool";
  const args = Object.entries(ev.args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
  d.innerHTML = `<summary>${esc(ev.tool)}</summary><pre>${esc(args)}</pre>`;
  els.chat.appendChild(d); lastTool = d; scrollDown();
}

function finishTool(ev) {
  const d = lastTool;
  if (!d) return;
  d.classList.add(ev.ok ? "ok" : "err");
  const pre = document.createElement("pre");
  pre.textContent = ev.summary || "";
  d.appendChild(pre);
  if (ev.image) { const img = new Image(); img.src = `data:image/png;base64,${ev.image}`; d.appendChild(img); }
  lastTool = null; scrollDown();
}

els.chat.addEventListener("click", (e) => {
  const a = e.target.closest("a");
  if (a) { e.preventDefault(); send({ type: "open_link", url: a.href }); }
});

// ---------------------------------------------------------------- confirmations
function showConfirm() {
  const c = state.confirmQueue[0];
  els.confirm.hidden = !c;
  if (c) { els.confirmText.textContent = c.summary; els.approve.focus(); }
}
function answerConfirm(approved, always = false) {
  const c = state.confirmQueue.shift();
  if (c) send({ type: "confirm_response", id: c.id, approved, always });
  showConfirm();
}
els.approve.onclick = () => answerConfirm(true);
els.always.onclick = () => answerConfirm(true, true);
els.deny.onclick = () => answerConfirm(false);

// ---------------------------------------------------------------- status pill
function setPill(text, cls = "") { els.pill.textContent = text; els.pill.className = `pill ${cls}`; }
function refreshPill() {
  if (state.listening === "recording" || state.listening === "hearing") return setPill("Listening…", "listening");
  if (state.listening === "transcribing") return setPill("Transcribing…", "thinking");
  if (state.listening === "loading_model") return setPill("Loading speech model…", "thinking");
  if (state.thinking) return setPill("Thinking…", "thinking");
  if (state.speaking) return setPill("Speaking", "speaking");
  const mode = state.server.listening_mode;
  setPill(mode === "wake" ? `Say “Hey ${state.server.assistant_name || "Shiv"}”` : "Ready");
}
function toggleBusy(on) { els.send.hidden = on; els.stop.hidden = !on; }
function setSpeaking(on) {
  state.speaking = on;
  send({ type: "avatar_speaking", on });
  refreshPill();
}

// ---------------------------------------------------------------- avatar adapters
class LocalAvatar {
  name = "local";
  get ready() { return true; }
  async start() {}
  speak(text) { send({ type: "speak_local", text }); }
  interrupt() { send({ type: "stop_speaking" }); }
  async stop() {}
}

class LiveAvatarAdapter {
  name = "liveavatar";
  session = null; token = null; starting = null; sdk = null; spokeCount = 0;
  get ready() { return !!this.session && this._live; }

  async start() {
    if (this.ready) return;
    if (this.starting) return this.starting;
    this.starting = (async () => {
      setPill("Connecting avatar…", "thinking");
      els.avatarToggle.textContent = "Connecting…";
      const r = await fetch("/api/avatar/session", { method: "POST", headers: { "x-shiv-token": TOKEN } });
      const data = await r.json();
      if (!r.ok || data.error) throw new Error(data.error || `HTTP ${r.status}`);
      this.sdk = this.sdk || (await import(LIVEAVATAR_SDK));
      const { LiveAvatarSession, SessionEvent, AgentEventsEnum } = this.sdk;
      this.token = data.session_token;
      const s = new LiveAvatarSession(this.token, { autoKeepAlive: true, voiceChat: false });  // no avatar-side mic: STT runs locally
      this.session = s;
      const streamReady = new Promise((res) => s.on(SessionEvent.SESSION_STREAM_READY, res));
      s.on(SessionEvent.SESSION_STREAM_READY, () => {
        console.info("liveavatar: stream ready");
        s.attach(els.video);
        wireAudio();
        els.stage.classList.add("live");
        ensurePlaying().then(() => console.info("liveavatar: after attach", videoState(), audioState()));
      });
      s.on(SessionEvent.SESSION_DISCONNECTED, (r) => { console.warn("liveavatar: disconnected", r); this._teardown("disconnected"); });
      s.on(AgentEventsEnum.AVATAR_SPEAK_STARTED, () => { this.spokeCount++; setSpeaking(true); console.info("liveavatar: speaking", videoState(), audioState()); });
      s.on(AgentEventsEnum.AVATAR_SPEAK_ENDED, () => { setSpeaking(false); bumpIdle(); });
      await s.start();
      await Promise.race([streamReady, new Promise((_, rej) => setTimeout(() => rej(new Error("stream timeout")), 20000))]);
      this._live = true;
      els.avatarToggle.textContent = "Disconnect avatar";
      if (data.sandbox) addNotice("LiveAvatar sandbox: free sessions end after ~1 minute.");
      refreshPill();
    })();
    const timeout = new Promise((_, rej) => setTimeout(() => rej(new Error("connection timed out")), 15000));
    try { await Promise.race([this.starting, timeout]); }
    catch (e) {
      console.warn("liveavatar: start failed:", e.message);
      const s = this.session; this._teardown();
      try { await s?.stop(); } catch {}
      throw e;
    } finally { this.starting = null; }
  }

  speak(text) { try { this.session.repeat(text); } catch (e) { console.warn(e); } }
  interrupt() { try { this.session?.interrupt(); } catch {} }

  async stop() {
    const s = this.session, token = this.token;
    this._teardown();
    try { await s?.stop(); } catch {}
    if (token) fetch("/api/avatar/stop", { method: "POST", headers: { "x-shiv-token": TOKEN, "content-type": "application/json" }, body: JSON.stringify({ session_token: token }) }).catch(() => {});
  }

  _teardown(reason) {
    this.session = null; this.token = null; this._live = false;
    els.stage.classList.remove("live");
    try { els.audio.pause(); els.audio.srcObject = null; els.video.srcObject = null; } catch {}
    els.soundBtn.hidden = true;
    els.avatarToggle.textContent = "Connect avatar";
    if (reason) addNotice(`Avatar ${reason}. I'll reconnect next time I speak.`);
    if (state.speaking) setSpeaking(false);
  }
}

const avatar = {
  local: new LocalAvatar(),
  live: new LiveAvatarAdapter(),
  liveBroken: false,
  get useLive() { return state.avatarPref === "auto" && state.server.avatar_provider === "liveavatar"; },
  interrupt() { this.live.interrupt(); this.local.interrupt(); },
};

let idleTimer = null;
function bumpIdle() {
  clearTimeout(idleTimer);
  const mins = Number(state.server.avatar_idle_minutes || 5);
  if (avatar.live.ready && mins > 0) idleTimer = setTimeout(() => { avatar.live.stop(); addNotice("Avatar paused while idle to save credits."); }, mins * 60000);
}

async function speak(text) {
  if (avatar.useLive && !avatar.liveBroken) {
    try {
      await avatar.live.start();
      const n = avatar.live.spokeCount;
      avatar.live.speak(text);
      bumpIdle();
      // Watchdog: if the avatar hasn't started talking in 6 s, its video/audio
      // link is broken — say it with the local voice instead.
      setTimeout(() => {
        if (avatar.live.spokeCount === n) {
          console.warn("liveavatar: no speech within 6s, falling back to local voice");
          addNotice("The live avatar isn't playing (video connection failed). Using the Windows voice for now — click Connect avatar to retry.");
          avatar.liveBroken = true;
          avatar.live.stop();
          if (state.server.tts_available) avatar.local.speak(text);
        }
      }, 6000);
      return;
    } catch (e) {
      console.warn("liveavatar start failed:", e?.message || e);
      if (/credit/i.test(e?.message || "")) {
        avatar.liveBroken = true;  // don't retry every reply; "Connect avatar" retries
        addNotice("LiveAvatar account is out of credits — using the local voice. Add credits, then click Connect avatar.");
      } else {
        addNotice(`Live avatar unavailable (${e.message}); using local voice.`);
      }
    }
  }
  if (state.server.tts_available) avatar.local.speak(text);
  else addNotice("No voice available — check the console log.");
}

els.avatarToggle.onclick = async () => {
  if (avatar.live.ready) return avatar.live.stop();
  avatar.liveBroken = false;
  try { await avatar.live.start(); avatar.live.speak(`Hi ${state.server.user_name || ""}, I'm here.`.replace(" ,", ",")); bumpIdle(); }
  catch (e) { addNotice(`Couldn't start avatar: ${e.message}`); refreshPill(); }
};

els.avatarSel.onchange = () => {
  state.avatarPref = els.avatarSel.value;
  if (state.avatarPref === "local" && avatar.live.ready) avatar.live.stop();
  applyState(state.server);
};

// ---------------------------------------------------------------- orb (local avatar)
(function orbLoop() {
  const c = els.orb, ctx = c.getContext("2d");
  let t = 0, level = 0;
  function frame() {
    const w = (c.width = c.clientWidth * devicePixelRatio), h = (c.height = c.clientHeight * devicePixelRatio);
    t += 0.016;
    const target = state.speaking ? 0.55 + 0.45 * Math.abs(Math.sin(t * 9) * Math.sin(t * 3.7))
      : state.thinking ? 0.25 + 0.1 * Math.sin(t * 4)
      : state.listening === "recording" || state.listening === "hearing" ? 0.35 : 0.08 + 0.04 * Math.sin(t * 1.5);
    level += (target - level) * 0.15;
    const r = Math.min(w, h) * (0.2 + level * 0.06), cx = w / 2, cy = h / 2;
    ctx.clearRect(0, 0, w, h);
    const halo = ctx.createRadialGradient(cx, cy, r * 0.4, cx, cy, r * (2 + level * 1.5));
    const hue = state.listening === "recording" ? "239,68,68" : state.thinking ? "139,92,246" : "88,101,242";
    halo.addColorStop(0, `rgba(${hue},${0.35 + level * 0.4})`); halo.addColorStop(1, `rgba(${hue},0)`);
    ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(cx, cy, r * 3, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath();
    for (let a = 0; a <= Math.PI * 2 + 0.01; a += Math.PI / 64) {
      const wob = 1 + level * 0.08 * Math.sin(a * 5 + t * 6) + 0.02 * Math.sin(a * 3 - t * 2);
      const x = cx + Math.cos(a) * r * wob, y = cy + Math.sin(a) * r * wob;
      a === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    const body = ctx.createRadialGradient(cx - r * 0.3, cy - r * 0.35, r * 0.1, cx, cy, r * 1.1);
    body.addColorStop(0, "#c7d2fe"); body.addColorStop(0.45, `rgb(${hue})`); body.addColorStop(1, "#1e1b4b");
    ctx.fillStyle = body; ctx.fill();
    requestAnimationFrame(frame);
  }
  frame();
})();

// ---------------------------------------------------------------- input
function submit() {
  const text = els.input.value.trim();
  if (!text) return;
  avatar.interrupt();
  send({ type: "user_message", text });
  els.input.value = ""; autosize();
}
function autosize() { els.input.style.height = "auto"; els.input.style.height = Math.min(els.input.scrollHeight, 140) + "px"; }
els.input.addEventListener("input", autosize);
els.input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } });
els.send.onclick = submit;
els.stop.onclick = () => { send({ type: "cancel" }); avatar.interrupt(); };
els.clear.onclick = () => { els.chat.innerHTML = ""; send({ type: "reset" }); };
els.provider.onchange = () => send({ type: "set_provider", provider: els.provider.value });
document.querySelectorAll(".listen-modes button").forEach((b) => (b.onclick = () => send({ type: "set_listening", mode: b.dataset.mode })));

// push-to-talk: hold the mic button (or a quick click toggles)
let pttDown = 0, pttOn = false;
function pttStart() { avatar.interrupt(); send({ type: "ptt_start" }); pttOn = true; }
function pttStop() { send({ type: "ptt_stop" }); pttOn = false; }
els.mic.addEventListener("pointerdown", () => { pttDown = Date.now(); if (!pttOn) pttStart(); else pttStop(); });
els.mic.addEventListener("pointerup", () => { if (pttOn && Date.now() - pttDown > 350) pttStop(); });
els.mic.addEventListener("pointerleave", () => { if (pttOn && pttDown && Date.now() - pttDown > 350) { pttStop(); pttDown = 0; } });
window.addEventListener("keydown", (e) => { if (e.key === "Escape") { send({ type: "cancel" }); avatar.interrupt(); } });

window.addEventListener("beforeunload", () => { if (avatar.live.ready) avatar.live.stop(); });
window.shivShutdown = () => avatar.live.stop();  // called by the desktop shell on quit

connect();
