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
  -- microphone is live, "Transcribing;" when it stops, and with --preview
  -- `Preview; "<json string>"` for each live revision. It finishes on SIGINT,
  -- cancels on SIGTERM, and writes the final transcript to stdout. The
  -- daemon's configuration applies.
  cmd = { voicekey_python, "-m", "voicekey", "--capture-to-stdout", "--client-name", "Neovim", "--preview" },
  -- Show the recording state as inline virtual text at the insertion point.
  marker = true,
  -- Show the live transcript there as you speak (virtual text, not buffer
  -- text); the final transcript replaces it.
  preview = true,
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

local function define_highlights()
  api.nvim_set_hl(0, "VoiceKeyPreview", { link = "Comment", default = true })
end
define_highlights()

local spaced

local function place(capture, row, col)
  local opts = { id = capture.mark, right_gravity = true }
  if config.preview and capture.preview then
    -- The final text is inserted as lines; the preview stays on this line.
    local text = spaced(capture.buf, row, col, capture.preview:gsub("%s*\n%s*", " "))
    if capture.state == "transcribing" then
      text = text .. " …"
    end
    opts.virt_text = { { text, "VoiceKeyPreview" } }
    opts.virt_text_pos = "inline"
  elseif config.marker then
    opts.virt_text = { { labels[capture.state], "Comment" } }
    opts.virt_text_pos = "inline"
  end
  capture.mark = api.nvim_buf_set_extmark(capture.buf, ns, row, col, opts)
end

local function redraw(capture)
  if not api.nvim_buf_is_valid(capture.buf) then
    return
  end
  local pos = api.nvim_buf_get_extmark_by_id(capture.buf, ns, capture.mark, {})
  if #pos > 0 then
    place(capture, pos[1], pos[2])
  end
end

local function set_state(capture, state)
  capture.state = state
  redraw(capture)
  vim.cmd.redrawstatus()
end

local function set_preview(capture, text)
  if type(text) ~= "string" then
    return
  end
  text = vim.trim((text:gsub("[%z\1-\8\11-\31\127]", "")))
  if text ~= "" and text ~= capture.preview then
    capture.preview = text
    redraw(capture)
  end
end

-- One stderr line from the capture command, on the main loop.
local function progress(capture, line)
  if active ~= capture then
    return
  end
  if line:find("^Preview; ") then
    local ok, text = pcall(vim.json.decode, line:sub(10))
    if ok then
      set_preview(capture, text)
    end
  elseif line:find("Transcribing;", 1, true) then
    if capture.state ~= "transcribing" then
      set_state(capture, "transcribing")
    end
  elseif line:find("Recording;", 1, true) and capture.state == "loading" then
    set_state(capture, "recording")
  end
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

function spaced(buf, row, col, text)
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

local function deliver(capture, result)
  local buf = capture.buf
  if not api.nvim_buf_is_valid(buf) then
    notify("buffer closed; " .. recovery_hint(), vim.log.levels.WARN)
    return
  end
  local pos = api.nvim_buf_get_extmark_by_id(buf, ns, capture.mark, {})
  api.nvim_buf_del_extmark(buf, ns, capture.mark)
  if result.signal ~= 0 and result.code == 0 then
    result.code = 128 + result.signal
  end
  if capture.cancelled then
    notify("cancelled")
    return
  end
  local text = prepare(result.stdout or "")
  if result.code ~= 0 or text == "" then
    local reason = result.code ~= 0 and ("exit " .. result.code) or "no speech"
    local detail = vim.trim(capture.stderr:match("[^\n]*voicekey capture:[^\n]*") or "")
    if detail:find("No such file", 1, true) or detail:find("Connection refused", 1, true) then
      detail = detail .. " (is voicekey.service running?)"
    end
    notify("no text (" .. reason .. ")" .. (detail ~= "" and ": " .. detail or ""), vim.log.levels.WARN)
    return
  end
  if #pos == 0 then
    notify("insertion point lost; " .. recovery_hint(), vim.log.levels.WARN)
    return
  end
  if not vim.bo[buf].modifiable then
    notify("buffer is not modifiable; " .. recovery_hint(), vim.log.levels.WARN)
    return
  end
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
    notify("insert failed (" .. err .. "); " .. recovery_hint(), vim.log.levels.WARN)
    return
  end
  local end_row = row + #lines - 1
  local end_col = (#lines == 1 and col or 0) + #lines[#lines]
  for _, win in ipairs(follow) do
    api.nvim_win_set_cursor(win, { end_row + 1, end_col })
  end
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
  -- stderr keeps non-preview lines for failure warnings; partial holds an unfinished line.
  local capture = { buf = buf, state = "loading", stderr = "", partial = "" }
  place(capture, insertion_point())
  active = capture
  local ok, proc = pcall(vim.system, config.cmd, {
    text = true,
    -- A stderr callback means vim.system does not collect stderr itself.
    -- Handle whole lines, since markers can be split across reads; keep all
    -- but previews for failure warnings.
    stderr = function(_, data)
      local chunk = capture.partial .. (data or "\n")
      local lines = vim.split(chunk, "\n", { plain = true })
      capture.partial = table.remove(lines)
      for _, line in ipairs(lines) do
        if not line:find("^Preview; ") then
          capture.stderr = capture.stderr .. line .. "\n"
        end
      end
      if #lines > 0 then
        vim.schedule(function()
          for _, line in ipairs(lines) do
            progress(capture, line)
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

local group = api.nvim_create_augroup("voicekey", { clear = true })
api.nvim_create_autocmd("ColorScheme", { group = group, callback = define_highlights })

-- Never leave the microphone recording after Neovim exits.
api.nvim_create_autocmd("VimLeavePre", {
  group = group,
  callback = function()
    if active then
      active.cancelled = true
      active.proc:kill("sigterm")
    end
  end,
})

return M
