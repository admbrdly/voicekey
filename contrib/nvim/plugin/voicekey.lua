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

-- While this Neovim has terminal focus, record its server address so that a
-- compositor keybinding (contrib/nvim/voicekey-route) can dictate into it
-- instead of sending input-method text to the terminal. Relies on the
-- terminal's focus reporting; set vim.g.voicekey_track_focus = false to skip.
if vim.g.voicekey_track_focus == false or vim.v.servername == "" then
  return
end

local runtime = vim.env.XDG_RUNTIME_DIR
if not runtime or runtime == "" then
  return
end
local dir = runtime .. "/voicekey"
local path = dir .. "/nvim-focus"

local function claim()
  if vim.fn.isdirectory(dir) == 0 and vim.fn.mkdir(dir, "p", tonumber("700", 8)) == 0 then
    return
  end
  local tmp = ("%s.%d"):format(path, vim.fn.getpid())
  if pcall(vim.fn.writefile, { vim.v.servername }, tmp) then
    vim.fn.setfperm(tmp, "rw-------")
    vim.uv.fs_rename(tmp, path)
  end
end

local function release()
  local ok, lines = pcall(vim.fn.readfile, path, "", 1)
  if ok and lines[1] == vim.v.servername then
    os.remove(path)
  end
end

local group = vim.api.nvim_create_augroup("voicekey_focus", { clear = true })
vim.api.nvim_create_autocmd({ "VimEnter", "FocusGained" }, { group = group, callback = claim })
vim.api.nvim_create_autocmd({ "FocusLost", "VimLeavePre" }, { group = group, callback = release })
if vim.v.vim_did_enter == 1 then
  claim()
end
