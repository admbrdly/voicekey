-- :VoiceKey [toggle|start|stop|cancel]; loads lua/voicekey.lua on first use.
if vim.g.loaded_voicekey then
  return
end
vim.g.loaded_voicekey = true

local actions = { "toggle", "start", "stop", "cancel" }

vim.api.nvim_create_user_command("VoiceKey", function(args)
  local action = args.args ~= "" and args.args or "toggle"
  if not vim.tbl_contains(actions, action) then
    vim.notify("voicekey: unknown action: " .. action, vim.log.levels.ERROR)
    return
  end
  require("voicekey")[action]()
end, {
  nargs = "?",
  complete = function()
    return actions
  end,
  desc = "Dictate into the buffer with voicekey",
})

-- Per-instance discovery for the daemon, plus the legacy router's focus file.
-- Terminal focus reports are evidence, not an atomic compositor tab identity.
if vim.g.voicekey_track_focus == false or vim.v.servername == "" then return end
local runtime = vim.env.XDG_RUNTIME_DIR
if not runtime or runtime == "" then return end
local dir = runtime .. "/voicekey"
local path = dir .. "/nvim-focus"
local registration = dir .. "/nvim/" .. vim.fn.getpid() .. ".json"
local terminal_focus = false
local sequence = 0
vim.g.voicekey_focused = false

local function report(focused)
  vim.g.voicekey_focused = focused
  sequence = sequence + 1
  vim.fn.mkdir(dir .. "/nvim", "p", tonumber("700", 8))
  local record = vim.json.encode({ pid = vim.fn.getpid(), server = vim.v.servername })
  local tmp = registration .. ".tmp"
  if pcall(vim.fn.writefile, { record }, tmp) then
    vim.fn.setfperm(tmp, "rw-------")
    vim.uv.fs_rename(tmp, registration)
  end
  -- Async local notification: no subprocess, prompt or blocked editor loop.
  local pipe, timer = vim.uv.new_pipe(false), vim.uv.new_timer()
  local function close()
    if not timer:is_closing() then timer:stop(); timer:close() end
    if not pipe:is_closing() then pipe:close() end
  end
  local message = vim.json.encode({ command = "editor-focus", args = {
    pid = vim.fn.getpid(), server = vim.v.servername, focused = focused,
    sequence = sequence,
  } }) .. "\n"
  timer:start(1500, 0, close)
  pipe:connect(dir .. "/control.sock", function(err)
    if err then close(); return end
    pipe:write(message, function() close() end)
  end)
  if focused then
    local legacy = path .. "." .. vim.fn.getpid()
    if pcall(vim.fn.writefile, { vim.v.servername }, legacy) then
      vim.fn.setfperm(legacy, "rw-------")
      vim.uv.fs_rename(legacy, path)
    end
  else
    local ok, lines = pcall(vim.fn.readfile, path, "", 1)
    if ok and lines[1] == vim.v.servername then os.remove(path) end
  end
end

local function gain()
  terminal_focus = true
  report(vim.bo.buftype == "")
end
local function lose()
  terminal_focus = false
  report(false)
end
local group = vim.api.nvim_create_augroup("voicekey_focus", { clear = true })
vim.api.nvim_create_autocmd({ "FocusGained", "VimResume" }, { group = group, callback = gain })
vim.api.nvim_create_autocmd({ "FocusLost", "VimSuspend" }, { group = group, callback = lose })
vim.api.nvim_create_autocmd({ "BufEnter", "TermEnter", "TermLeave" }, { group = group, callback = function()
  local focused = terminal_focus and vim.bo.buftype == ""
  if focused ~= vim.g.voicekey_focused then report(focused) end
end })
vim.api.nvim_create_autocmd("VimLeavePre", { group = group, callback = function()
  lose()
  os.remove(registration)
end })
vim.api.nvim_create_autocmd("VimEnter", { group = group, callback = function() report(terminal_focus and vim.bo.buftype == "") end })
if vim.v.vim_did_enter == 1 then report(false) end
