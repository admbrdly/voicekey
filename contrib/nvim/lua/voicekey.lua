-- voicekey.lua — dictate into the current Neovim buffer.
--
-- Runs `python -m voicekey --capture-to-stdout` and inserts its transcript as
-- buffer text with nvim_buf_set_text. Nothing is typed, so the editor mode
-- cannot turn dictated words into commands, and no evdev access or input
-- method is involved. The command is a thin client of the running voicekey
-- daemon: it shares the daemon's loaded models and configuration, and
-- transcripts go to the daemon's journal (`python -m voicekey --last`). Requires
-- Neovim 0.10 and a running daemon with client capture support.
if vim.fn.has("nvim-0.10") == 0 then
  error("voicekey.lua requires Neovim 0.10 or newer")
end

local M = {}

local api = vim.api
local ns = api.nvim_create_namespace("voicekey")
local voicekey_python = vim.fn.expand("~/.local/share/voicekey/venv/bin/python")

local config = {
  -- Client of the running daemon: prints "Recording;" on stderr once the
  -- microphone is live, "Transcribing;" when it stops, finishes on SIGINT,
  -- cancels on SIGTERM, and writes
  -- the transcript to stdout. The daemon's configuration applies.
  cmd = { voicekey_python, "-m", "voicekey", "--capture-to-stdout", "--client-name", "Neovim" },
  -- Show the recording state as inline virtual text at the insertion point.
  marker = true,
  -- Add a space between the transcript and adjacent words.
  spacing = true,
  notify = true,
}

local function recovery_hint()
  local python = config.cmd[2] == "-m" and config.cmd[3] == "voicekey" and config.cmd[1] or voicekey_python
  return "recover it with `" .. vim.fn.shellescape(python)
    .. " -m voicekey --last` or the same command with `--copy-last`"
end

-- One capture at a time: { proc, buf, mark, state }.
local active

local labels = {
  loading = "[voicekey: loading]",
  recording = "[voicekey: recording]",
  transcribing = "[voicekey: transcribing]",
}

local function notify(msg, level)
  if config.notify or (level or vim.log.levels.INFO) >= vim.log.levels.WARN then
    vim.notify("voicekey: " .. msg, level)
  end
end

local function place(capture, row, col)
  local opts = { id = capture.mark, right_gravity = true }
  if config.marker or capture.preview ~= nil then
    opts.virt_text = { { capture.preview or labels[capture.state] or "", "Comment" } }
    opts.virt_text_pos = "inline"
  end
  capture.mark = api.nvim_buf_set_extmark(capture.buf, ns, row, col, opts)
end

local function set_state(capture, state)
  capture.state = state
  if not api.nvim_buf_is_valid(capture.buf) then
    return
  end
  local pos = api.nvim_buf_get_extmark_by_id(capture.buf, ns, capture.mark, {})
  if #pos > 0 then
    place(capture, pos[1], pos[2])
  end
  vim.cmd.redrawstatus()
end

-- Normal mode inserts after the cursor character, like `a`; insert mode at the cursor.
local function insertion_point()
  local row, col = unpack(api.nvim_win_get_cursor(0))
  local mode = api.nvim_get_mode().mode
  if not mode:find("^i") and not mode:find("^R") then
    local line = api.nvim_get_current_line()
    local char = vim.fn.matchstr(line:sub(col + 1), "^.")
    col = col + #char
  end
  return row - 1, col
end

-- Transcript as buffer lines: CR and other control characters removed, outer space trimmed.
local function prepare(text)
  text = text:gsub("\r\n?", "\n"):gsub("[%z\1-\8\11-\31\127]", ""):gsub("^%s+", ""):gsub("%s+$", "")
  return text
end

local function spaced(buf, row, col, text)
  if not config.spacing then
    return text
  end
  local line = api.nvim_buf_get_lines(buf, row, row + 1, false)[1] or ""
  local before, after = line:sub(col, col), line:sub(col + 1, col + 1)
  if before ~= "" and not before:match("[%s%(%[{\"'`]") then
    text = " " .. text
  end
  if after ~= "" and after:match("[%w]") then
    text = text .. " "
  end
  return text
end

-- Shared by client capture and daemon pins; never selects a window/buffer.
local function position(capture)
  if not capture or not api.nvim_buf_is_valid(capture.buf) then
    return nil, "buffer closed"
  end
  if not api.nvim_buf_is_loaded(capture.buf) then return nil, "buffer unloaded" end
  if not vim.bo[capture.buf].modifiable then return nil, "buffer is not modifiable" end
  local pos = api.nvim_buf_get_extmark_by_id(capture.buf, ns, capture.mark, {})
  if #pos == 0 then return nil, "insertion point lost" end
  return pos
end

local function put(capture, text, keep_pin)
  local pos, reason = position(capture)
  if not pos then return false, reason end
  local buf = capture.buf
  local row, col = pos[1], pos[2]
  text = spaced(buf, row, col, text)
  local lines = vim.split(text, "\n", { plain = true })
  -- Keep a cursor that sits exactly at the insertion point after the new text.
  local follow = {}
  for _, win in ipairs(vim.fn.win_findbuf(buf)) do
    local cursor = api.nvim_win_get_cursor(win)
    if cursor[1] - 1 == row and cursor[2] == col then
      table.insert(follow, win)
    end
  end
  local ok, err = pcall(api.nvim_buf_set_text, buf, row, col, row, col, lines)
  if not ok then
    return false, "insert failed (" .. err .. ")", true
  end
  local end_row = row + #lines - 1
  local end_col = (#lines == 1 and col or 0) + #lines[#lines]
  if keep_pin then
    capture.preview = ""
    place(capture, end_row, end_col)
  else
    api.nvim_buf_del_extmark(buf, ns, capture.mark)
  end
  for _, win in ipairs(follow) do
    -- Cursor housekeeping must not turn a confirmed buffer edit into a retry.
    pcall(api.nvim_win_set_cursor, win, { end_row + 1, end_col })
  end
  return true
end

local function remove(capture)
  if capture and api.nvim_buf_is_valid(capture.buf) then
    pcall(api.nvim_buf_del_extmark, capture.buf, ns, capture.mark)
  end
end

local function deliver(capture, result)
  if result.signal ~= 0 and result.code == 0 then result.code = 128 + result.signal end
  if capture.cancelled then remove(capture); notify("cancelled"); return end
  local text = prepare(result.stdout or "")
  if result.code ~= 0 or text == "" then
    remove(capture)
    local reason = result.code ~= 0 and ("exit " .. result.code) or "no speech"
    local detail = vim.trim(capture.stderr:match("[^\n]*voicekey capture:[^\n]*") or "")
    if detail:find("No such file", 1, true) or detail:find("Connection refused", 1, true) then
      detail = detail .. " (is voicekey.service running?)"
    end
    notify("no text (" .. reason .. ")" .. (detail ~= "" and ": " .. detail or ""), vim.log.levels.WARN)
    return
  end
  local ok, reason = put(capture, text, false)
  if not ok then
    remove(capture)
    notify(reason .. "; " .. recovery_hint(), vim.log.levels.WARN)
  end
end

-- Daemon protocol. Requests carry a wall-clock expiry and insertions additionally
-- carry the journal's revocable permission file and unique operation ID.
local pins, operations = {}, {}
local function now()
  local seconds, micros = vim.uv.gettimeofday()
  return seconds + micros / 1000000
end
local function refused(reason) return { status = "refused", reason = reason } end

local function dispatch(request)
  local method, args = request.method, request.args
  if type(request.expires) ~= "number" or now() >= request.expires then
    return refused("Neovim request expired")
  end
  -- Bound caches: abandoned pins expire after a day; operation results are kept
  -- through their execution deadline, after which the request itself is refused.
  for id, pin in pairs(pins) do
    if now() - pin.used > 86400 then remove(pin); pins[id] = nil end
  end
  for id, op in pairs(operations) do if now() > op.expires then operations[id] = nil end end
  if method == "status" then
    return { status = "ok", pid = vim.fn.getpid(), server = vim.v.servername,
      focused = vim.g.voicekey_focused == true }
  elseif method == "pin" then
    if vim.g.voicekey_focused ~= true then return refused("Neovim is not focused") end
    if active then return refused("Neovim client capture is active") end
    local buf = api.nvim_get_current_buf()
    if not vim.bo[buf].modifiable or vim.bo[buf].buftype ~= "" then
      return refused("buffer is not an editable text buffer")
    end
    local mode = api.nvim_get_mode().mode
    if mode:find("^no") or mode:find("^[vV]") or mode:byte() == 22 then
      return refused("selection or operator pending")
    end
    if not pins[args.id] then
      local pin = { buf = buf, used = now(), preview = "[voicekey: listening]" }
      place(pin, insertion_point())
      pins[args.id] = pin
    end
    local pin = pins[args.id]
    local pos, reason = position(pin)
    if not pos then return refused(reason) end
    local line = api.nvim_buf_get_lines(buf, pos[1], pos[1] + 1, false)[1]
    return { status = "ok", before = line:sub(1, pos[2]), buffer = api.nvim_buf_get_name(buf) }
  elseif method == "unpin" then
    remove(pins[args.id]); pins[args.id] = nil
    return { status = "ok" }
  end
  if method == "insert" and operations[args.operation] then return operations[args.operation].reply end
  local pin = pins[args.id]
  local pos, reason = position(pin)
  if not pos then return refused(reason) end
  pin.used = now()
  if method == "check" then return { status = "ok" } end
  if method == "preview" then
    pin.preview = (args.text or ""):gsub("[\r\n]", " ↵ ")
    place(pin, pos[1], pos[2])
    return { status = "ok" }
  elseif method == "insert" then
    if args.permit and args.permit ~= vim.NIL and vim.fn.filereadable(args.permit) ~= 1 then return refused("insertion cancelled") end
    local text = prepare(args.text)
    if now() >= request.expires then return refused("insertion expired") end
    local ran, ok, why, uncertain = pcall(put, pin, text, args.keep_pin)
    if not ran then why, ok, uncertain = tostring(ok), false, true end
    local reply = ok and { status = "ok" } or { status = uncertain and "unknown" or "refused", reason = why }
    operations[args.operation] = { reply = reply, expires = request.expires }
    if not args.keep_pin then remove(pin); pins[args.id] = nil end
    return reply
  end
  return refused("unknown Neovim method")
end

function M.rpc(payload)
  local ok, reply = pcall(function() return dispatch(vim.json.decode(payload)) end)
  return vim.json.encode(ok and reply or { status = "unknown", reason = tostring(reply) })
end

--- Start recording; the transcript lands at the current cursor position.
function M.start()
  if active then
    return
  end
  if vim.fn.executable(config.cmd[1]) ~= 1 then
    notify("not executable: " .. config.cmd[1], vim.log.levels.ERROR)
    return
  end
  local buf = api.nvim_get_current_buf()
  if not vim.bo[buf].modifiable then
    notify("buffer is not modifiable", vim.log.levels.WARN)
    return
  end
  local capture = { buf = buf, state = "loading", stderr = "" }
  place(capture, insertion_point())
  active = capture
  local ok, proc = pcall(vim.system, config.cmd, {
    text = true,
    -- A stderr callback means vim.system does not collect stderr itself:
    -- Keep it for failure warnings and progress markers, including markers
    -- split across reads or an external stop followed by slow transcription.
    stderr = function(_, data)
      capture.stderr = capture.stderr .. (data or "")
      local transcribing = capture.stderr:find("Transcribing;", 1, true)
      if transcribing or capture.stderr:find("Recording;", 1, true) then
        vim.schedule(function()
          if active ~= capture then return end
          if transcribing and capture.state ~= "transcribing" then
            set_state(capture, "transcribing")
          elseif capture.state == "loading" then
            set_state(capture, "recording")
          end
        end)
      end
    end,
  }, function(result)
    vim.schedule(function()
      if active == capture then
        active = nil
      end
      deliver(capture, result)
      vim.cmd.redrawstatus()
    end)
  end)
  if not ok then
    active = nil
    api.nvim_buf_del_extmark(buf, ns, capture.mark)
    notify("could not start: " .. proc, vim.log.levels.ERROR)
    return
  end
  capture.proc = proc
  vim.cmd.redrawstatus()
end

--- Finish recording and insert the transcript (SIGINT).
function M.stop()
  if active and active.state ~= "transcribing" then
    set_state(active, "transcribing")
    active.proc:kill("sigint")
  end
end

--- Discard the recording (SIGTERM); nothing is inserted.
function M.cancel()
  if active then
    active.cancelled = true
    active.proc:kill("sigterm")
  end
end

function M.toggle()
  if active then
    M.stop()
  else
    M.start()
  end
end

--- "loading", "recording", "transcribing", or nil; for statuslines.
function M.status()
  return active and active.state or nil
end

function M.setup(opts)
  config = vim.tbl_deep_extend("force", config, opts or {})
  if opts and opts.cmd then
    config.cmd = opts.cmd
  end
end

-- Never leave the microphone recording after Neovim exits.
api.nvim_create_autocmd("VimLeavePre", {
  group = api.nvim_create_augroup("voicekey", { clear = true }),
  callback = function()
    if active then
      active.cancelled = true
      active.proc:kill("sigterm")
    end
  end,
})

return M
