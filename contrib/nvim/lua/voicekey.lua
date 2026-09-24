-- voicekey.lua — dictate into the current Neovim buffer.
--
-- Runs `python -m voicekey --capture-to-stdout` and inserts its transcript as
-- buffer text with nvim_buf_set_text. Nothing is typed, so the editor mode
-- cannot turn dictated words into commands, and no daemon, evdev access or
-- input method is involved. Requires Neovim 0.10.
if vim.fn.has("nvim-0.10") == 0 then
  error("voicekey.lua requires Neovim 0.10 or newer")
end

local M = {}

local api = vim.api
local ns = api.nvim_create_namespace("voicekey")

local config = {
  -- Command that records until SIGINT and writes the transcript to stdout.
  cmd = { vim.fn.expand("~/.local/share/voicekey/venv/bin/python"), "-m", "voicekey", "--capture-to-stdout" },
  -- Show the recording state as inline virtual text at the insertion point.
  marker = true,
  -- Add a space between the transcript and adjacent words.
  spacing = true,
  notify = true,
}

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
  if config.marker then
    opts.virt_text = { { labels[capture.state], "Comment" } }
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

local function deliver(capture, result)
  local buf = capture.buf
  if not api.nvim_buf_is_valid(buf) then
    notify("buffer closed; transcript kept in ~/.local/state/voicekey/stdout/", vim.log.levels.WARN)
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
    local detail = vim.trim((result.stderr or ""):match("[^\n]*voicekey capture:[^\n]*") or "")
    notify("no text (" .. reason .. ")" .. (detail ~= "" and ": " .. detail or ""), vim.log.levels.WARN)
    return
  end
  if #pos == 0 then
    notify("insertion point lost; transcript kept in ~/.local/state/voicekey/stdout/", vim.log.levels.WARN)
    return
  end
  if not vim.bo[buf].modifiable then
    notify("buffer is not modifiable; transcript kept in ~/.local/state/voicekey/stdout/", vim.log.levels.WARN)
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
    notify("insert failed (" .. err .. "); transcript kept in ~/.local/state/voicekey/stdout/", vim.log.levels.WARN)
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
  local capture = { buf = buf, state = "loading" }
  place(capture, insertion_point())
  active = capture
  local ok, proc = pcall(vim.system, config.cmd, {
    text = true,
    stderr = function(_, data)
      if data and data:find("Recording;", 1, true) then
        vim.schedule(function()
          if active == capture and capture.state == "loading" then
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
