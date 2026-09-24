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
