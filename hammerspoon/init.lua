-- SenseVoice push-to-talk dictation
--
--   Hold  right ⌘  → talk, release to insert text     (push-to-talk)
--   Tap   right ⌘  → start, tap again to finish       (toggle, for long form)
--
-- Right Command is still a live modifier: pressing any other key while it is
-- held (⌘C, ⌘V, …) cancels the pending dictation instead of transcribing.

require("hs.ipc")  -- enables the `hs` command-line tool for diagnostics

local SOCKET = "/tmp/dictation.sock"
local DICT_DIR = os.getenv("HOME") .. "/dictation"
local PYTHON = DICT_DIR .. "/venv/bin/python3"

local TAP_SECONDS = 0.35   -- shorter than this counts as a tap, not a hold
local HOTKEY_PATH = DICT_DIR .. "/hotkey.json"

-- D1 (zero content loss) recovery machinery, 2026-07-26 incident: a STOP
-- reply that is empty/missing used to be a silent no-op (paste() just
-- returned) — his words were transcribed server-side but never reached the
-- screen, and his only recourse was to re-speak the whole thing.
--
-- REJECTED DESIGN (2026-07-26, caught before shipping): matching "the newest
-- .wav in recordings/" against a freshness window (e.g. "written within the
-- last 30s") to guess which take an empty STOP reply belongs to, then
-- auto-pasting whatever RETRY returns for it. Measured against his real
-- usage this is unsafe: 33% of his last 198 takes are <30s apart and the
-- shortest gaps are 3s, so a recency window regularly matches a NEIGHBOURING
-- take, not the one that just failed — silently pasting the wrong take's
-- text is worse than pasting nothing (it's silently plausible, not visibly
-- wrong). Structurally, no client-side recency/mtime heuristic can be made
-- safe here even with a tighter window: server.py's segment_worker() saves
-- one .wav PER SEGMENT of a long recording on a background thread, outside
-- the STOP request's own lock hold (server.py ~2103), so multiple unrelated
-- recordings can legitimately land in RECORDINGS_DIR within seconds of each
-- other. The wire protocol also doesn't help: STOP's reply is bare text with
-- no audio filename (server.py transcribe() `return text`, ~2435), so the
-- client has no authoritative identifier for "the take I just ended" at all.
--
-- What's used instead: Tinho already has a browser dashboard (editor.py,
-- 127.0.0.1:8765) whose history list shows each take's raw/text + a 🔄 重跑
-- (re-run) button wired to the same RETRY <name> socket command, keyed by
-- the exact `audio` field from that take's own history.jsonl record — i.e.
-- identity resolved by a human recognizing his own words, not by a client-
-- side guess. On an empty STOP reply, the client fronts that dashboard
-- (openRecoveryDashboard(), used by finishRecording() and by ⌘⌥⌃R) instead
-- of maintaining a parallel, weaker, guess-based re-run path. If a client-
-- side one-click recovery is wanted later, the correct fix is a protocol
-- change (STOP's reply carrying its own audio filename) — flagged for the
-- server.py owner, not worked around here.
local CLIENT_LOG = os.getenv("HOME") .. "/.hammerspoon/dictation_client.log"
local DASHBOARD_URL = "http://127.0.0.1:8765/"

-- Every control that starts dictation. A list, not one binding: the keyboard
-- shortcut and the mouse button are both live at once.
triggers = {{type = "modifier", keycode = 54, label = "右 ⌘"}}

local function loadTrigger()
  local f = io.open(HOTKEY_PATH, "r")
  if not f then return end
  local raw = f:read("*a"); f:close()
  local ok, cfg = pcall(hs.json.decode, raw)
  if ok and cfg and cfg.triggers and #cfg.triggers > 0 then
    triggers = cfg.triggers
  end
end
loadTrigger()

local function modsMatch(want, have)
  want = want or {}
  return (want.cmd or false) == (have.cmd or false)
     and (want.alt or false) == (have.alt or false)
     and (want.ctrl or false) == (have.ctrl or false)
     and (want.shift or false) == (have.shift or false)
end

-- kind "modifier"/"mouse": id is keycode/button. kind "key": id is {code, mods}.
local function matchesTrigger(kind, id)
  for _, t in ipairs(triggers) do
    if t.type == kind then
      if kind == "modifier" and t.keycode == id then return true end
      if kind == "mouse" and t.button == id then return true end
      if kind == "key" and t.keycode == id.code and modsMatch(t.mods, id.mods) then
        return true
      end
    end
  end
  return false
end

-- F13–F19: distinctive keys that collide with nothing, ideal as the target of a
-- Logi Options+ keystroke assignment for a mouse button.
local FKEY_NAMES = {[105] = "F13", [107] = "F14", [113] = "F15", [106] = "F16",
                    [64] = "F17", [79] = "F18", [80] = "F19"}

local serverTask = nil
local recording = false
local toggleMode = false
local cancelled = false
local pressTime = 0
local menu = hs.menubar.new()
-- Whether the server acked the most recent START with "ok". Read (not
-- guessed at) — it's literally the server's own reply to the START WE just
-- sent for the take currently in progress. Used only to make an empty STOP
-- reply's alert more precise ("probably never started" vs "server had it
-- but produced nothing"); never used to decide what to paste.
local lastTakeStartAcked = false
-- Bumped once per beginRecording() call; lets the 1.2s start-ack watchdog
-- (below) tell "this take's ack still hasn't landed" apart from "a new take
-- already started/ended since this watchdog was scheduled" on a fast tap.
local takeGeneration = 0

-- Blocking socket I/O must NEVER run inside an eventtap callback: macOS
-- disables a tap whose callback overruns, which silently kills dictation until
-- Hammerspoon is reloaded. Everything on the hotkey path goes through this
-- async form; `send` (synchronous) is only for menu actions.
local function sendAsync(cmd, callback)
  local out = {}
  -- exitCode is passed through (2026-08-03) so callers can tell "server
  -- replied with nothing" apart from "the connection itself was reset" --
  -- e.g. `nc` exits nonzero when the peer (server.py) dies mid-request
  -- (a powerd SIGTERM mid-recording, see finishRecording below), which
  -- reads very differently to Tinho than an ordinary empty reply.
  local t = hs.task.new("/bin/sh", function(exitCode, stdout)
    if callback then
      callback((stdout or ""):gsub("^%s*(.-)%s*$", "%1"), exitCode)
    end
  -- -w 300: with rolling segments STOP normally replies in seconds, but if a
  -- backlog of segments is still transcribing the reply must never be thrown
  -- away — a discarded reply reads as "it lost my dictation".
  end, {"-c", string.format("printf %%s '%s' | /usr/bin/nc -U -w 300 %s", cmd, SOCKET)})
  t:start()
  return t
end

local function send(cmd)
  local out, ok = hs.execute(
    string.format("printf %%s '%s' | /usr/bin/nc -U -w 10 %s", cmd, SOCKET)
  )
  if not ok then return nil end
  return (out:gsub("^%s*(.-)%s*$", "%1"))
end

local function setIcon(text)
  if menu then menu:setTitle(text) end
end

--------------------------------------------------------------------------------
-- On-screen indicator
--
-- The menubar icon alone is useless while dictating: you are looking at the
-- text field, not the top of the screen. This draws a pill near the bottom of
-- whichever screen currently has the mouse.
--------------------------------------------------------------------------------

-- Minimal indicator per Tinho's spec: no text, no timer — a small quiet pill
-- low on the screen: a soft blinking dot and a fine-line waveform that moves
-- with the voice. Proportions kept deliberately restrained (thin 2px bars,
-- tight radii) — it should read as a system element, not a widget.
local PILL_H = 22
local BAR_N = 16
local BAR_W, BAR_GAP = 2, 3
local DOT_X = 12
local BAR_X0 = 22
local PILL_W = BAR_X0 + BAR_N * (BAR_W + BAR_GAP) - BAR_GAP + 10
local LEVEL_PATH = "/tmp/dictation.level"
indicator = nil          -- global: canvases get collected as locals
pulseTimer = nil
levelTimer = nil

local function destroyIndicator()
  if pulseTimer then pulseTimer:stop(); pulseTimer = nil end
  if levelTimer then levelTimer:stop(); levelTimer = nil end
  if indicator then indicator:delete(); indicator = nil end
end

local function showIndicator(dotColor, withWave)
  destroyIndicator()
  local screen = hs.mouse.getCurrentScreen() or hs.screen.mainScreen()
  local f = screen:frame()
  local frame = {
    x = f.x + (f.w - PILL_W) / 2,
    y = f.y + f.h - 44,        -- low, just above the very bottom edge
    w = PILL_W,
    h = PILL_H,
  }
  indicator = hs.canvas.new(frame)
  indicator:appendElements(
    {
      type = "rectangle",
      action = "fill",
      roundedRectRadii = {xRadius = PILL_H / 2, yRadius = PILL_H / 2},
      fillColor = {red = 0.06, green = 0.06, blue = 0.07, alpha = 0.78},
    },
    {
      type = "circle",
      action = "fill",
      center = {x = DOT_X, y = PILL_H / 2},
      radius = 3,
      fillColor = dotColor,
    }
  )
  for i = 1, BAR_N do
    indicator:appendElements({
      type = "rectangle",
      action = "fill",
      roundedRectRadii = {xRadius = 1, yRadius = 1},
      frame = {x = BAR_X0 + (i - 1) * (BAR_W + BAR_GAP),
               y = PILL_H / 2 - 1, w = BAR_W, h = 2},
      fillColor = {white = 1, alpha = 0.9},
    })
  end
  indicator:level(hs.canvas.windowLevels.overlay)
  indicator:behavior(hs.canvas.windowBehaviors.canJoinAllSpaces)
  indicator:show()

  -- slow, gentle breathing rather than a hard blink
  local up, alpha = false, 1.0
  pulseTimer = hs.timer.doEvery(0.05, function()
    alpha = alpha + (up and 0.035 or -0.035)
    if alpha <= 0.45 then alpha, up = 0.45, true end
    if alpha >= 1.0 then alpha, up = 1.0, false end
    if indicator then
      indicator[2].fillColor = {red = dotColor.red, green = dotColor.green,
                                blue = dotColor.blue, alpha = alpha}
    end
  end)

  if withWave then
    local levels = {}
    local smooth = 0
    levelTimer = hs.timer.doEvery(0.07, function()
      if not indicator then return end
      local lv = 0
      local fh = io.open(LEVEL_PATH, "r")
      if fh then lv = tonumber(fh:read("*a")) or 0; fh:close() end
      smooth = smooth * 0.55 + lv * 0.45          -- soften jumps between reads
      table.insert(levels, smooth)
      if #levels > BAR_N then table.remove(levels, 1) end
      for i = 1, BAR_N do
        local v = levels[i] or 0
        local h = math.min(14, 2 + v * 220)       -- speech rms ~0.02-0.08
        indicator[2 + i].frame = {x = BAR_X0 + (i - 1) * (BAR_W + BAR_GAP),
                                  y = PILL_H / 2 - h / 2, w = BAR_W, h = h}
      end
    end)
  end
end

-- exposed so the indicator can be previewed from the `hs` CLI without dictating
dictation = dictation or {}
dictation.preview = function(kind)
  if kind == "transcribing" then
    showIndicator({red = 1.0, green = 0.72, blue = 0.2, alpha = 1}, false)
  elseif kind == "off" then
    destroyIndicator()
  else
    showIndicator({red = 0.95, green = 0.25, blue = 0.25, alpha = 1}, true)
  end
end

--------------------------------------------------------------------------------
-- One-click engine comparison: records once, runs the sample through both
-- engines, opens the result. No terminal needed.
--------------------------------------------------------------------------------

local COMPARE_SECONDS = 12
local RESULT_PATH = DICT_DIR .. "/compare_result.txt"
compareTask = nil

dictation = dictation or {}
dictation.compare = function()
  if compareTask and compareTask:isRunning() then
    hs.alert.show("對比測試已經跑緊")
    return
  end
  if recording then
    hs.alert.show("而家錄緊音，等陣先")
    return
  end

  local function countdown(n)
    if n > 0 then
      hs.alert.show(string.format("%d 秒後開始講嘢…", n), 1)
      hs.timer.doAfter(1, function() countdown(n - 1) end)
      return
    end
    hs.alert.show(string.format("🔴 講嘢！錄緊 %d 秒…", COMPARE_SECONDS), 2)
    hs.timer.doAfter(COMPARE_SECONDS, function()
      hs.alert.show("兩個引擎分析緊…（約 30 秒）", 3)
    end)
    compareTask = hs.task.new("/bin/sh", function(code)
      destroyIndicator()
      if code == 0 then
        hs.execute("open -e " .. RESULT_PATH)
      else
        hs.alert.show("對比失敗，睇 " .. RESULT_PATH)
        hs.execute("open -e " .. RESULT_PATH)
      end
    end, {"-c", string.format("cd %s && ./venv/bin/python3 compare.py %d > %s 2>&1",
      DICT_DIR, COMPARE_SECONDS, RESULT_PATH)})
    compareTask:start()
  end
  countdown(3)
end

local RED = {red = 0.95, green = 0.25, blue = 0.25, alpha = 1}
local AMBER = {red = 1.0, green = 0.72, blue = 0.2, alpha = 1}

local function indicateRecording(isToggle)
  showIndicator(RED, true)     -- blinking red dot + live waveform, nothing else
end

local function indicateTranscribing()
  showIndicator(AMBER, false)  -- amber dot, waveform frozen: processing
end

local function serverAlive()
  return send("PING") == "pong"
end

local function startServer()
  if serverTask and serverTask:isRunning() then return end
  -- serverTask is nil after hs.reload() even though the process it spawned is
  -- still alive; spawning another would give us two servers fighting over the
  -- socket and the mic. Ask the socket instead of trusting our own handle.
  if serverAlive() then setIcon("🎙️") return end
  setIcon("🎙️…")
  -- INCIDENT 2026-07-27: spawning server.py directly from Hammerspoon gave the
  -- process NONE of the environment in com.huitinho.dictation.plist (the
  -- DICTATION_* lock and model settings), and it raced launchd, which then
  -- respawned forever logging "another server instance is already running".
  -- Two owners for one service IS the bug. launchd is the single owner now;
  -- Hammerspoon only asks it to (re)start, so the plist environment always
  -- applies and there is exactly one starter.
  serverTask = hs.task.new("/bin/launchctl", function(code)
    if code ~= 0 then
      setIcon("🎙️✗")
      hs.notify.new({title = "Dictation", informativeText = "launchctl kickstart failed (" .. code .. ")"}):send()
    end
  end, {"kickstart", "-k", "gui/" .. hs.processInfo.userID .. "/com.huitinho.dictation"})
  serverTask:start()
  -- launchctl returns immediately; the socket is the real readiness signal.
  hs.timer.waitUntil(function() return send("PING") == "pong" end,
                     function() setIcon("🎙️") end, 0.3)
end

-- Auto-learn: after pasting, quietly watch the focused text field through the
-- Accessibility API (the same mechanism Wispr Flow used — Hammerspoon already
-- holds the permission). If the user corrects a word before sending, the diff
-- is submitted to the server's AI gatekeeper and the dictionary updates
-- itself. No copying, no dashboard visit, nothing extra to do.
--
-- ELECTRON/WEB-VIEW FIX (2026-09-23). Measured cause of zero learn events
-- since 2026-07-29: in Electron/Chromium apps (Claude desktop confirmed live
-- — `AXFocusedUIElement` resolves but its `AXValue` is nil), the focused
-- node's own AXValue is frequently empty even though the node IS the right
-- one and IS updating. resolveFocusedText() below tries a chain of
-- STRUCTURAL strategies (element role, not a tuned number) before giving up:
-- direct AXValue (native AppKit fields — TextEdit/Notes, unchanged from
-- before) -> AXSelectedText (some Chromium editable roles keep this live
-- even when AXValue is empty) -> a bounded descent into the focused node's
-- own children looking for a text-bearing role (AXWebArea/AXGroup wrapping
-- AXStaticText run nodes is the common Chromium contenteditable shape).
--
-- SAFETY, measured live against this Mac's own Hammerspoon + Chrome/Electron
-- (2026-09-23, read-only, no mic/model/service touched):
--   - ax.systemWideElement():attributeValue("AXFocusedUIElement") is FAST —
--     this is the ORIGINAL, already-safe entry point (same one this code
--     used before), kept as the only way this function asks "what has
--     focus". Confirmed fast even when it returns nil.
--   - ax.applicationElement(app):attributeValue("AXFocusedUIElement") — a
--     DIFFERENT entry point that asks a specific (possibly non-frontmost)
--     app for its own focus state — HUNG for 2+ minutes against a live
--     Chrome process on this Mac. NEVER use this form here.
--   - :parameterizedAttributeValue(...) (e.g. AXStringForRange) also hung.
--     NEVER use a parameterized/range AX call in this poll.
--   - Plain attributeValue() calls (AXValue/AXSelectedText/AXChildren/
--     AXRole) on a handle already obtained from systemWideElement are fast
--     and are the only calls resolveFocusedText() makes.
-- Every AX call below is wrapped in pcall and the whole resolve is bounded
-- in depth/breadth (a resource cap, not a correctness gate — the gate that
-- decides "did we find text" is always role + non-empty string).
local function resolveFocusedText(ax)
  local focused = ax.systemWideElement():attributeValue("AXFocusedUIElement")
  if not focused then return nil end

  -- Strategy 1: native text fields (TextEdit, Notes, most AppKit apps) —
  -- the original, working case, unchanged.
  local direct = focused:attributeValue("AXValue")
  if type(direct) == "string" and #direct > 0 then return direct end

  -- Strategy 2: some Chromium/Electron editable roles keep AXValue empty
  -- but still expose the live selection/composition text.
  local sel = focused:attributeValue("AXSelectedText")
  if type(sel) == "string" and #sel > 0 then return sel end

  -- Strategy 3: Chromium/Electron contenteditable fields often carry the
  -- real text on DESCENDANTS of the focused node, not on the node itself.
  -- Walk children of the already-obtained focused handle (no new
  -- cross-process focus query, so the hang above does not apply) looking
  -- for the first text-bearing role. Depth/breadth bounds are a resource
  -- cap only.
  local TEXT_ROLES = {
    AXTextArea = true, AXTextField = true, AXStaticText = true, AXWebArea = true,
  }
  local function descend(el, depth)
    if not el or depth > 6 then return nil end
    local ok, role = pcall(function() return el:attributeValue("AXRole") end)
    if ok and TEXT_ROLES[role] then
      local vok, v = pcall(function() return el:attributeValue("AXValue") end)
      if vok and type(v) == "string" and #v > 0 then return v end
    end
    local kok, kids = pcall(function() return el:attributeValue("AXChildren") end)
    if kok and kids then
      local acc = nil
      for i, k in ipairs(kids) do
        if i > 40 then break end
        local found = descend(k, depth + 1)
        if found and found ~= "" then
          acc = acc and (acc .. found) or found
        end
      end
      if acc and #acc > 0 then return acc end
    end
    return nil
  end
  return descend(focused, 0)
end

learnTimer = nil
local function watchEdits(pastedText)
  if not pastedText or #pastedText < 12 then return end
  local ok, ax = pcall(require, "hs.axuielement")
  if not ok then return end
  if learnTimer then learnTimer:stop(); learnTimer = nil end
  -- byte-level probes from both ends: still recognisable after partial edits
  local p1 = pastedText:sub(1, 24)
  local p2 = pastedText:sub(-24)
  local lastSeen, checks = nil, 0
  learnTimer = hs.timer.doEvery(3, function()
    checks = checks + 1
    local rok, cur = pcall(resolveFocusedText, ax)
    if not rok then cur = nil end
    local related = type(cur) == "string" and #cur > 6
      and (cur:find(p1, 1, true) or cur:find(p2, 1, true))
    if related then lastSeen = cur end
    -- field cleared / focus moved away (message sent) or 90s passed: settle up
    if (not related and lastSeen) or checks >= 30 then
      if learnTimer then learnTimer:stop(); learnTimer = nil end
      if lastSeen and lastSeen ~= pastedText then
        hs.http.asyncPost("http://127.0.0.1:8765/api/learn",
          hs.json.encode({pasted = pastedText, edited = lastSeen}),
          {["Content-Type"] = "application/json"},
          function(code, body)
            if code ~= 200 then return end
            local okd, r = pcall(hs.json.decode, body or "")
            if okd and r and r.accepted and #r.accepted > 0 then
              local pair = r.accepted[1]
              hs.alert.show("📖 已學會：" .. pair[1] .. " → " .. pair[2], 2)
            end
          end)
      end
    end
  end)
end

-- Append-only, small local record of failed/recovered takes — the client used
-- to record nothing about a failed take, which is why the 2026-07-26 incident
-- could not be reconstructed after the fact. Never raises: a logging failure
-- must never be why a recovery attempt itself gets aborted.
local function clientLog(line)
  local ok = pcall(function()
    local f = io.open(CLIENT_LOG, "a")
    if not f then return end
    f:write(os.date("!%Y-%m-%dT%H:%M:%SZ") .. " " .. line .. "\n")
    f:close()
  end)
  if not ok then print("[dictation] clientLog failed: " .. line) end
end

local function paste(text)
  if not text or text == "" then return end
  local saved = hs.pasteboard.getContents()
  hs.pasteboard.setContents(text)
  hs.eventtap.keyStroke({"cmd"}, "v", 0)
  hs.timer.doAfter(0.4, function()
    if saved then hs.pasteboard.setContents(saved) end
  end)
  watchEdits(text)
end

local function beginRecording()
  -- No liveness pre-check here: it was a synchronous round-trip on the hotkey
  -- path. If the server is down START simply no-ops — but (2026-07-26) that
  -- no longer stays silent: the async ack below warns as soon as it's known,
  -- which is normally within a second, i.e. while he's likely still talking
  -- and can stop early instead of only finding out empty-handed at STOP time.
  --
  -- HARD DEADLINE ON THE ACK ITSELF (2026-08-03): the socket read behind
  -- sendAsync waits up to 300s (nc -w 300, deliberately generous for STOP's
  -- transcription backlog case — never shorten that one). If START's own
  -- server-side handler is queued behind a stuck request_lock, the ack can
  -- arrive very late and still say "ok" -- silently correct, but only after
  -- however long Tinho already spent talking into a session that had not
  -- actually started yet (incident 2026-08-02: a stuck lock delayed START's
  -- ack by ~40s; by the time it cleared, START and the already-pending STOP
  -- both fired back-to-back, "recorded wall=0.0s" -- he never got a
  -- real-time signal that nothing was being captured). A LATE "ok" is not
  -- the same claim as an ON-TIME "ok": this watchdog fires within 1.2s if
  -- no ack has landed yet, so he finds out while he can still just let go
  -- and press the key again, instead of minutes later. Deliberately visible
  -- two ways -- hs.alert (in his eyeline near the cursor) AND hs.notify (in
  -- Notification Center, so it survives even if he's not looking at the
  -- screen right then) -- because a single transient HUD line is exactly
  -- what got missed 2026-08-02.
  recording = true
  cancelled = false
  lastTakeStartAcked = false
  setIcon("🔴")
  indicateRecording(false)
  takeGeneration = takeGeneration + 1
  local thisGeneration = takeGeneration
  hs.timer.doAfter(1.2, function()
    if recording and not lastTakeStartAcked and thisGeneration == takeGeneration then
      clientLog("start ack=(pending) outcome=start-not-acked-1.2s-watchdog")
      setIcon("⚠️")
      hs.alert.show("⚠️ 而家錄唔到 — 撳返一下重試", 6)
      hs.notify.new({title = "Dictation", informativeText = "而家錄唔到，撳返一下重試"}):send()
    end
  end)
  -- RETRY AN UNACKED START ONCE (2026-08-10). The single largest cause of
  -- lost takes is not the microphone — it is that the server was RESTARTING
  -- at the moment Tinho pressed the key. Every config change ships as a
  -- launchd restart, and each restart is a ~10s window in which a START gets
  -- no ack and the whole take is lost with no way to recover it. Measured in
  -- dictation_client.log: `start ack="" outcome=start-not-acked`, immediately
  -- followed by `stop_reply_len=0 ... outcome=pending-manual-pick`.
  --
  -- The server comes back within seconds and the socket is recreated on
  -- bootstrap, so a second attempt lands. Retrying converts an outage into a
  -- sub-second delay, and it is safe by construction: START is idempotent on
  -- the server (it opens a take; a second START while one is open is a
  -- no-op), and `thisGeneration` makes sure a retry from an abandoned take
  -- can never resurrect it after Tinho released the key.
  local function sendStart(attempt)
    sendAsync("START", function(reply)
      -- `recording` guards against this firing after the take already ended
      -- (STOP already fired, possibly on a fast tap/hold) — don't warn about a
      -- take that isn't in progress any more.
      if reply == "ok" then
        lastTakeStartAcked = true
        if recording then setIcon("🔴") end  -- undo the watchdog's ⚠️ if this
                                              -- (late) ack still lands mid-take
        -- LIVE CAPTURE PROBE (2026-08-15). Tinho: "I told you our system
        -- should tell me immediately if it cannot record anything. But why
        -- will it show that it is recording... when it is not recording?"
        --
        -- START being acked only proves the server accepted the take, not
        -- that CoreAudio is delivering frames into it. Until now the only
        -- zero-frame check ran in transcribe(), AFTER release — so a dead
        -- InputStream let this pill sit there looking healthy for as long as
        -- he held the key, and he found out he had wasted 30 seconds of
        -- speech only when he let go.
        --
        -- One shot at +1.2s: long enough that a slow first callback is not
        -- mistaken for a dead stream, short enough that at most about a
        -- second of speech is lost. Generation-guarded so a probe from an
        -- abandoned take cannot kill a later one.
        --
        -- SIGNAL, NOT COUNT (2026-08-22). Tinho: "If it cannot pick up any
        -- words that I say, why don't it let me know immediately." The
        -- 2026-08-15 probe above only asked whether FRAMES had arrived, and a
        -- wedged Bluetooth mic delivers callbacks of exact zeros forever — so
        -- the server answered "yes" and he kept talking into nothing anyway.
        -- The server now classifies the audio itself and replies
        -- "<yes|no>\t<state>\t<rms>\t<peak>\t<n>"; an OLD server still replies
        -- a bare "yes"/"no", which falls through to the legacy branch below
        -- unchanged.
        --
        -- Two failures, two different remedies, so deliberately two different
        -- alerts and two different timings:
        --
        --   silent  -> the mic is delivering digital silence. Nothing he says
        --              is being recorded and nothing he does with his voice
        --              will fix it. Warn AT ONCE (1.2s) and tell him to stop.
        --              Zero false positives by construction: a real mic, even
        --              the built-in one in a silent room, never delivers
        --              samples that are all exactly 0.0.
        --   low     -> real audio, but nothing in the window is speech-loud.
        --              This one CANNOT be judged in 1.2s: at that point he may
        --              simply not have started speaking yet, and a warning
        --              that fires on his thinking pauses is a warning he
        --              learns to ignore, which protects nothing. Measured
        --              2026-08-22 by replaying his own 514 saved recordings:
        --              alerting on ONE low window would have fired on 17.6% of
        --              takes that transcribed perfectly; requiring THREE in a
        --              row (1.2s, 4.0s, 7.0s, with no healthy window in
        --              between) drops that to 1 in 163 (0.6%) while still
        --              catching 57% of takes attenuated to the input-volume
        --              24/100 level behind the 2026-07-29 incident. Only time
        --              separates "too quiet" from "not talking yet" — no
        --              threshold does. A take that ends before 7s simply never
        --              gets the third probe and is never warned about.
        --
        -- At most one alert per take.
        local capWarned = false
        local capLowRun = 0
        -- `delay` is relative to now; `atSeconds` is how far into the take
        -- the answer describes, for the log line only.
        local function probeCapture(delay, atSeconds)
          hs.timer.doAfter(delay, function()
            if not (recording and thisGeneration == takeGeneration) then return end
            sendAsync("CAPTURING", function(cap)
              if not (recording and thisGeneration == takeGeneration) then return end
              if capWarned then return end
              cap = cap or ""
              local legacy, state = cap:match("^([^\t]*)\t?([^\t]*)")
              if state == "" then state = nil end  -- old server: yes/no only
              if state == nil then
                -- LEGACY PATH, byte-identical to the 2026-08-15 behaviour.
                -- Also the "not one frame arrived" case on the new server,
                -- which still answers the bare string "no" on purpose.
                if legacy == "no" then
                  capWarned = true
                  clientLog("live-capture probe: no frames "
                    .. tostring(atSeconds) .. "s after START — "
                    .. "aborting the take so he stops talking")
                  setIcon("🎙️✗")
                  hs.alert.show("⚠️ 收唔到聲 — 唔好講落去，撳返一次重試", 4)
                end
                return
              end
              if state == "silent" then
                capWarned = true
                clientLog("live-capture probe: digital silence at "
                  .. tostring(atSeconds) .. "s (" .. cap
                  .. ") — mic is wedged, aborting so he stops talking")
                setIcon("🎙️✗")
                hs.alert.show(
                  "⚠️ 咪死咗 — 收到嘅全部係靜音，唔好講落去，撳返一次重試", 5)
              elseif state == "low" then
                capLowRun = capLowRun + 1
                if capLowRun >= 3 then
                  capWarned = true
                  clientLog("live-capture probe: 3 consecutive low windows "
                    .. "by " .. tostring(atSeconds) .. "s (" .. cap
                    .. ") — input is too quiet, warning him")
                  setIcon("🎙️🔉")
                  hs.alert.show(
                    "⚠️ 收到你把聲但係太細 — 行近啲支咪，或者校大 input 音量", 5)
                elseif capLowRun == 1 then
                  probeCapture(2.8, 4.0)   -- 1.2s -> 4.0s
                else
                  probeCapture(3.0, 7.0)   -- 4.0s -> 7.0s
                end
              else
                -- "ok": the microphone is working. Reset the run so a quiet
                -- passage in the middle of a good take can never accumulate
                -- into a warning across the whole take.
                capLowRun = 0
              end
            end)
          end)
        end
        probeCapture(1.2, 1.2)
        if attempt > 1 then
          clientLog(string.format("start acked on attempt %d — "
            .. "server was almost certainly restarting", attempt))
        end
      elseif recording and thisGeneration == takeGeneration and attempt < 3 then
        clientLog(string.format("start ack=%q attempt=%d — retrying",
                                reply, attempt))
        hs.timer.doAfter(0.4, function()
          if recording and not lastTakeStartAcked
              and thisGeneration == takeGeneration then
            sendStart(attempt + 1)
          end
        end)
      elseif recording then
        clientLog(string.format("start ack=%q outcome=start-not-acked "
          .. "after %d attempts", reply, attempt))
        hs.alert.show("⚠️ Server 未確認開始錄音，講嘅嘢可能唔會錄到", 3)
      end
    end)
  end
  sendStart(1)
  return true
end

-- Front Tinho's already-existing recovery UI (editor.py's dashboard) instead
-- of a client-side guess-based re-run: its history list is keyed by each
-- take's own history.jsonl `audio` field and its 🔄 重跑 button already calls
-- the same RETRY <name> socket command — identity resolved by him
-- recognizing his own words in a specific row, not by client-side recency
-- matching (see the REJECTED DESIGN note near CLIENT_LOG above for why a
-- guess was unsafe). Bound to ⌘⌥⌃R and mirrored in the menu; both call this
-- so there is exactly one code path, and it reuses the one already tested.
local UI_ACTIONS_LOG = os.getenv("HOME") .. "/dictation/ui_actions.log"

-- Single choke point for opening this dashboard: log caller + timestamp so a
-- future "why did this open" question is a grep, not an evening of
-- archaeology across server.py/watchdog.py/editor.py/this file (2026-08-02).
local function openRecoveryDashboard(reason)
  local f = io.open(UI_ACTIONS_LOG, "a")
  if f then
    f:write(string.format("%s dashboard-opened reason=%s\n",
      os.date("!%Y-%m-%dT%H:%M:%SZ"), reason or "unspecified"))
    f:close()
  end
  hs.execute("open " .. DASHBOARD_URL)
end
hs.hotkey.bind({"cmd", "alt", "ctrl"}, "R", function() openRecoveryDashboard("hotkey") end)

local function finishRecording(discard)
  if not recording then return end
  recording = false
  toggleMode = false
  setIcon(discard and "🎙️" or "⏳")
  if discard then destroyIndicator() else indicateTranscribing() end
  local takeId = os.date("!%Y%m%d-%H%M%S")
  local startAcked = lastTakeStartAcked
  sendAsync("STOP", function(text, exitCode)
    setIcon("🎙️")
    destroyIndicator()
    if discard then return end
    -- 2026-07-26: STOP's reply now carries the take's own audio name so a
    -- failed take can be recovered without guessing which .wav it was:
    -- "<name_or_->\t<text>". Split it off — the name is metadata and must
    -- never reach the pasteboard (it leaked into his text once, which is
    -- exactly why this is parsed rather than assumed absent).
    local audioName, body = nil, text
    if text then
      local n, rest = text:match("^([^\t\n]*)\t(.*)$")
      if n then
        audioName, body = n, rest
      elseif text:match("^%-$") or text:match("^[%w][%w%-_.]*%.wav$") then
        -- ROOT CAUSE of Tinho's 「有時錄音後佢會出 `-`，完全出唔到字」
        -- (2026-08-05). The server always sends "<name_or_->\t<text>", but
        -- when <text> is empty the reply is "-\t" — and readSocket's
        -- `gsub("^%s*(.-)%s*$", "%1")` trims trailing whitespace, which
        -- includes that tab. The pattern above then finds no tab, the
        -- metadata falls through as `body`, and the bare "-" (or, with a
        -- saved take, the .wav filename) gets pasted as if it were his
        -- words. This is the same metadata leak the comment above says was
        -- already fixed once — it was only fixed for the non-empty-text
        -- case. An empty-text reply must reach the alert path below, never
        -- the pasteboard.
        audioName, body = text, ""
      end
    end
    lastTakeAudioName = audioName
    if body and body ~= "" then
      paste(body)
      return
    end
    -- D1 (zero content loss): an empty/missing STOP reply is NEVER a silent
    -- no-op any more (2026-07-26 incident — server produced text but it
    -- never reached the screen, no signal, only recourse was re-speaking).
    -- paste() is never called with empty text, so the clipboard is never
    -- touched on this path — nothing to restore, nothing clobbered.
    --
    -- No client-side auto-RETRY here (see the REJECTED DESIGN note near
    -- CLIENT_LOG): the client has no safe way to identify which .wav
    -- belongs to THIS take, so it never guesses and never auto-pastes a
    -- maybe-wrong take's text. Recovery via the dashboard is still one
    -- keystroke away (⌘⌥⌃R / menu), but this no longer force-fronts a
    -- browser window on every empty reply (Tinho, 2026-08-02: "super
    -- annoying... even dictation fail, don't jump away") — a silent jump
    -- away from whatever he was doing was worse than the recovery
    -- convenience it bought.
    --
    -- CONNECTION-RESET CASE (2026-08-03 incident): nc exits nonzero when
    -- the peer died mid-request instead of replying (server.py's own
    -- SIGTERM handler already emergency-saves the in-progress audio for
    -- exactly this case — see _emergency_flush's INCIDENT 2026-07-22
    -- comment). This reads completely differently to Tinho than "server
    -- ran fine and produced nothing" -- say so explicitly, since a system
    -- event interrupting the recording (not a transcription failure) is
    -- exactly the distinction he asked for after losing a take to it.
    local resetByServer = exitCode and exitCode ~= 0
    clientLog(string.format(
      "take=%s stop_reply_len=0 start_acked=%s exit_code=%s recovery=alert-only " ..
      "outcome=pending-manual-pick",
      takeId, tostring(startAcked), tostring(exitCode)))
    if resetByServer then
      hs.alert.show("⚠️ 錄音期間服務中斷 — audio 已存底，撳 ⌘⌥⌃R 揀返呢段", 6)
      hs.notify.new({title = "Dictation",
        informativeText = "錄音期間服務中斷，audio 已存底，撳 ⌘⌥⌃R 揀返呢段"}):send()
    elseif startAcked then
      hs.alert.show("⚠️ 冇收到文字 — 撳 ⌘⌥⌃R 揀返呢段「🔄 重跑」", 4)
    else
      hs.alert.show("⚠️ 冇收到文字，可能未開始錄音 — 撳 ⌘⌥⌃R 核實，或重新錄音", 4)
    end
  end)
end

-- cancel a pending hold-dictation when right ⌘ is being used as a real modifier
-- NOTE: eventtaps must be stored globally. As locals they get garbage-collected
-- once init.lua finishes and silently stop delivering events.
keyWatcher = hs.eventtap.new({hs.eventtap.event.types.keyDown}, function(e)
  -- a configured key trigger is not a "stray keypress"; don't let it cancel a hold
  if matchesTrigger("key", {code = e:getKeyCode(), mods = e:getFlags()}) then
    return false
  end
  if recording and not toggleMode then
    cancelled = true
    finishRecording(true)
  end
  return false
end)
keyWatcher:start()

-- Shared by the keyboard and mouse watchers so both triggers behave identically.
local function onTrigger(down)
  print(string.format("[dictation] trigger %s (recording=%s toggle=%s)",
    down and "down" or "up", tostring(recording), tostring(toggleMode)))

  if down then
    if toggleMode then
      finishRecording(false)      -- second tap ends a toggle session
    else
      pressTime = hs.timer.secondsSinceEpoch()
      beginRecording()
    end
  else
    if cancelled or not recording then return end
    if hs.timer.secondsSinceEpoch() - pressTime < TAP_SECONDS then
      toggleMode = true           -- it was a tap: keep recording until next tap
      setIcon("🔴∞")
      indicateRecording(true)
    else
      finishRecording(false)      -- it was a hold: release ends it
    end
  end
end

-- Capture mode: the dashboard asks for the next press so the user can pick a
-- trigger by pressing it, instead of looking up keycodes.
capturing = false
local function saveTrigger(cfg)
  for _, t in ipairs(triggers) do   -- already bound: nothing to do
    if t.type == cfg.type and t.keycode == cfg.keycode
       and t.button == cfg.button then
      hs.alert.show(cfg.label .. " 已經綁咗")
      return
    end
  end
  table.insert(triggers, cfg)
  local f = io.open(HOTKEY_PATH, "w")
  if not f then return end
  f:write(hs.json.encode({triggers = triggers}, true))
  f:close()
  hs.alert.show("已加觸發鍵：" .. cfg.label)
end

dictation = dictation or {}
dictation.removeTrigger = function(index)
  if #triggers <= 1 then return false end   -- never leave zero ways to dictate
  table.remove(triggers, tonumber(index))
  local f = io.open(HOTKEY_PATH, "w")
  if not f then return false end
  f:write(hs.json.encode({triggers = triggers}, true))
  f:close()
  return true
end

local MOD_NAMES = {[54] = "右 ⌘", [55] = "左 ⌘", [58] = "左 ⌥", [61] = "右 ⌥",
                   [59] = "左 ⌃", [62] = "右 ⌃", [56] = "左 ⇧", [60] = "右 ⇧"}

flagWatcher = hs.eventtap.new({hs.eventtap.event.types.flagsChanged}, function(e)
  local code = e:getKeyCode()
  if capturing then
    if MOD_NAMES[code] then
      capturing = false
      saveTrigger({type = "modifier", keycode = code,
                   label = MOD_NAMES[code]})
    end
    return false
  end
  if not matchesTrigger("modifier", code) then return false end
  -- a modifier's flag is set on press and cleared on release
  local flags = e:getFlags()
  local held = flags.cmd or flags.alt or flags.ctrl or flags.shift
  onTrigger(held and true or false)
  return false
end)
flagWatcher:start()

-- Handles plain-key triggers (F13 etc.) from a Logi keystroke assignment. A
-- keystroke assignment sends down+up as one quick tap, so this naturally drives
-- toggle mode: tap to start, tap to stop. Defined here — after onTrigger and
-- saveTrigger — because its callback closes over both.
keyTriggerWatcher = hs.eventtap.new(
  {hs.eventtap.event.types.keyDown, hs.eventtap.event.types.keyUp},
  function(e)
    local code = e:getKeyCode()
    local mods = e:getFlags()
    local isDown = e:getType() == hs.eventtap.event.types.keyDown
    if capturing and isDown then
      -- capture any real key (with whatever modifiers are held), so a Logi combo
      -- like ⌃⌥⌘\ is recorded exactly as it will arrive
      local parts = {}
      if mods.ctrl then parts[#parts+1] = "⌃" end
      if mods.alt then parts[#parts+1] = "⌥" end
      if mods.shift then parts[#parts+1] = "⇧" end
      if mods.cmd then parts[#parts+1] = "⌘" end
      local base = FKEY_NAMES[code] or hs.keycodes.map[code] or ("鍵" .. code)
      parts[#parts+1] = string.upper(base)
      capturing = false
      saveTrigger({type = "key", keycode = code,
                   mods = {cmd = mods.cmd or false, alt = mods.alt or false,
                           ctrl = mods.ctrl or false, shift = mods.shift or false},
                   label = table.concat(parts)})
      return true
    end
    if not matchesTrigger("key", {code = code, mods = mods}) then return false end
    onTrigger(isDown)
    return true
  end)
keyTriggerWatcher:start()

mouseWatcher = hs.eventtap.new(
  {hs.eventtap.event.types.otherMouseDown, hs.eventtap.event.types.otherMouseUp},
  function(e)
    local btn = e:getProperty(hs.eventtap.event.properties.mouseEventButtonNumber)
    local isDown = (e:getType() == hs.eventtap.event.types.otherMouseDown)
    if capturing then
      if isDown then
        capturing = false
        saveTrigger({type = "mouse", button = btn,
                     label = "滑鼠掣 " .. tostring(btn)})
      end
      return true   -- swallow the click that was only meant to pick the trigger
    end
    if not matchesTrigger("mouse", btn) then return false end
    onTrigger(isDown)
    return true     -- the button is ours now; don't let apps also act on it
  end)
mouseWatcher:start()

dictation = dictation or {}
dictation.capture = function()
  capturing = true
  hs.alert.show("撳你想加嘅掣：修飾鍵、滑鼠掣、或 F13–F19", 4)
end
dictation.reloadTrigger = loadTrigger

-- last N transcriptions, newest first, as a clickable copy-to-clipboard menu
local function recentMenu()
  local items = {}
  local f = io.open(DICT_DIR .. "/history.jsonl", "r")
  if not f then
    return {{title = "（仲未有記錄）", disabled = true}}
  end
  local lines = {}
  for line in f:lines() do lines[#lines + 1] = line end
  f:close()

  for i = #lines, math.max(1, #lines - 14), -1 do
    local ok, entry = pcall(hs.json.decode, lines[i])
    if ok and entry and entry.text then
      local label = entry.text:gsub("%s+", " ")
      if utf8.len(label) and utf8.len(label) > 42 then
        label = label:sub(1, utf8.offset(label, 43) - 1) .. "…"
      end
      local clock = (entry.time or ""):match("T(%d%d:%d%d)") or ""
      items[#items + 1] = {
        title = clock .. "  " .. label,
        fn = function()
          hs.pasteboard.setContents(entry.text)
          hs.alert.show("已複製，⌘V 貼上")
        end,
      }
    end
  end
  if #items == 0 then
    return {{title = "（仲未有記錄）", disabled = true}}
  end
  items[#items + 1] = {title = "-"}
  items[#items + 1] = {title = "開啟完整記錄檔…", fn = function()
    hs.execute("open -e " .. DICT_DIR .. "/history.jsonl")
  end}
  return items
end

local function setLang(code)
  send("LANG " .. code)
  hs.notify.new({title = "Dictation", informativeText = "Language: " .. code}):send()
end

local function setPolish(on)
  send("POLISH " .. (on and "on" or "off"))
  hs.notify.new({title = "Dictation", informativeText = "潤色: " .. (on and "開" or "關")}):send()
end

-- One-button "fix dictation" (Tinho, 2026-08-03): pressing this IS the
-- explicit, in-person joint-test go-ahead the safe-testing protocol asks
-- for -- a deliberate click, at a moment of his choosing, same authority as
-- saying "fix dictation" out loud. Unlike "重啟 server" (a plain restart),
-- this captures a diagnostic bundle FIRST (thread count, recent logs,
-- deployed commit, incident trend, watchdog log) via
-- ~/ops/fix_dictation_now.sh, so whatever caused the failure is preserved
-- even though the restart that follows clears it from the live process.
-- This does NOT diagnose or auto-patch the root cause -- that still needs
-- real investigation each time (tonight's leak and its own regression both
-- took real reading of code and log history to find and fix correctly; a
-- script that guessed and rewrote live server.py would be more dangerous
-- than the failure it cured). The bundle is what makes that next
-- investigation start from a file instead of from zero.
local function fixDictationNow()
  hs.alert.show("🩹 修復緊…", 2)
  hs.task.new("/bin/bash",
    function(code, _stdout, stderr)
      if code == 0 then
        hs.alert.show("✅ 已重啟，診斷資料已存低", 3)
        hs.notify.new({title = "Dictation",
          informativeText = "已修復並存低診斷資料喺 last-fix-bundle.txt"}):send()
      else
        hs.alert.show("⚠️ 修復 script 出錯 — 睇 last-fix-bundle.txt", 4)
        clientLog("fix_dictation_now.sh failed rc=" .. tostring(code)
          .. " stderr=" .. tostring(stderr))
      end
    end,
    {os.getenv("HOME") .. "/ops/fix_dictation_now.sh"}):start()
end

-- Revert to a known-good tagged version (Tinho, 2026-08-04): same static
-- gates as deploy_dictation.sh/fix_dictation_now.sh (syntax + pyflakes +
-- import check BEFORE anything touches live, backup + restore on any
-- failure). dictionary.json is data, not code -- reverting it means
-- losing any rule learned since that tag, which the script flags loudly
-- rather than doing silently. This IS the explicit joint-test go-ahead.
local function revertDictationTo(tag, label)
  hs.alert.show("↩️ 回退緊去 " .. label .. "…", 2)
  hs.task.new("/bin/bash",
    function(code, _stdout, stderr)
      if code == 0 then
        hs.alert.show("✅ 已回退去 " .. label, 3)
        hs.notify.new({title = "Dictation",
          informativeText = "已回退去 " .. label}):send()
      else
        hs.alert.show("⚠️ 回退去 " .. label .. " 失敗 — live 未受影響", 5)
        clientLog("dictation_revert.sh " .. tag .. " failed rc=" .. tostring(code)
          .. " stderr=" .. tostring(stderr))
      end
    end,
    {os.getenv("HOME") .. "/ops/dictation_revert.sh", tag}):start()
end

-- Rollback menu entries are GENERATED, never hand-maintained (Tinho,
-- 2026-08-05: "whenever you add a version, you add to here"). A list derived
-- from the tags themselves cannot drift. dictation_revert.sh resolves local
-- tags first, remote as fallback.
--
-- LEGACY_REVERT_TAGS retired (Tinho-approved, 2026-08-17): the 3 hardcoded
-- entries pointed at the archived tinho-dictation repo and were never used;
-- keeping them would defeat the point of the cap below.
--
-- ROLLBACK_MENU_CAP (Tinho-approved, 2026-08-17): the menu had grown to 13
-- entries (3 frozen legacy + 10 accumulated monorepo tags) and none had ever
-- been clicked. Hard-capped here so the menu can never re-bloat no matter
-- how many tags accumulate in the repo going forward -- this cap is the
-- actual enforcement point, not a promise to tag less by hand.
local ROLLBACK_MENU_CAP = 3
local CN_NUM = {"一","二","三","四","五","六","七","八","九","十",
                "十一","十二","十三","十四","十五"}

local function revertMenuItems()
  local items = {}
  local seen = {}
  local function add(tag, note)
    if tag == "" or seen[tag] then return end
    seen[tag] = true
    local label = "版本" .. (CN_NUM[#items + 1] or tostring(#items + 1))
    items[#items + 1] = {
      title = "↩️ 回退去 " .. label .. " (" .. tag .. ")" .. (note or ""),
      fn = function() revertDictationTo(tag, label) end,
    }
  end
  -- local snapshot tags, oldest first, so numbering stays chronological;
  -- `tail -n` after the ascending sort keeps only the CAP newest tags.
  -- `git tag --sort=creatordate`, not `for-each-ref --format='%(...)'`: the
  -- format string's %(...) is a shell-quoting hazard through hs.execute for no
  -- gain, and plain `tag` already prints exactly the short names we want.
  local out = hs.execute(
    "/usr/bin/git -C " .. os.getenv("HOME") ..
    "/dictation tag --sort=creatordate 2>/dev/null | tail -n " .. ROLLBACK_MENU_CAP)
  for tag in (out or ""):gmatch("[^\r\n]+") do add((tag:gsub("%s+$", "")), " ⟵ 現行系列") end
  return items
end

if menu then
  setIcon("🎙️✗")
  menu:setMenu(function()
    local m = {
      {title = "撳住右⌘講嘢 · 輕撳一下 = 長段落模式", disabled = true},
      {title = "-"},
      {title = "語言：auto (粵+英)", fn = function() setLang("auto") end},
      {title = "語言：粵語", fn = function() setLang("yue") end},
      {title = "語言：English", fn = function() setLang("en") end},
      {title = "-"},
      {title = "潤色：開", fn = function() setPolish(true) end},
      {title = "潤色：關（原始轉寫）", fn = function() setPolish(false) end},
      {title = "-"},
      {title = "🕘 最近轉寫（撳一下複製）", menu = recentMenu()},
      {title = "🔁 開啟記錄面板（重跑／覆核） (⌘⌥⌃R)", fn = function() openRecoveryDashboard("menu") end},
      {title = "📖 編輯字典…", fn = function()
        hs.execute("open http://127.0.0.1:8765/")
      end},
      {title = "-"},
      {title = "重啟 server", fn = startServer},
      {title = "🩹 修復語音輸入", fn = fixDictationNow},
    }
    for _, item in ipairs(revertMenuItems()) do m[#m + 1] = item end
    m[#m + 1] = {title = "重載設定", fn = hs.reload}
    return m
  end)
end

-- Safety net: macOS silently disables an eventtap whose callback overran, and
-- a disabled tap looks identical to a working one until you press the key. Poll
-- and restart them rather than waiting for the user to notice dictation died.
tapWatchdog = hs.timer.doEvery(5, function()
  local revived = false
  if flagWatcher and not flagWatcher:isEnabled() then
    flagWatcher:start(); revived = true
  end
  if keyWatcher and not keyWatcher:isEnabled() then
    keyWatcher:start(); revived = true
  end
  if mouseWatcher and not mouseWatcher:isEnabled() then
    mouseWatcher:start(); revived = true
  end
  if keyTriggerWatcher and not keyTriggerWatcher:isEnabled() then
    keyTriggerWatcher:start(); revived = true
  end
  if revived then
    recording, toggleMode = false, false
    destroyIndicator()
    setIcon("🎙️")
    print("[dictation] eventtap was disabled by macOS — restarted")
  end
end)

startServer()
hs.notify.new({title = "Dictation", informativeText = "撳住右⌘講嘢"}):send()
