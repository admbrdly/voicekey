# voicekey.bash — let voicekey-route dictate at bash's prompt.
#
# Source it from ~/.bashrc, after anything else that sets PS0 or
# PROMPT_COMMAND (starship, bash-preexec). While bash waits at its prompt the
# terminal title starts with $VOICEKEY_PROMPT_MARK; while a command runs it
# does not. voicekey-route starts the daemon only when it sees the mark, and
# the daemon's input-method text lands in the command line. Pressing Enter
# stops that dictation before the command runs: it waits for pending speech,
# then discards anything still queued for the terminal, so no dictated text
# reaches the program you started.
#
# The mark is printed from PS1. Ghostty's shell integration appends its own
# title to PS1 at every prompt, so its title feature is turned off for this
# shell (it checks GHOSTTY_SHELL_FEATURES each time).
#
# Limit: in vi command mode (after Esc) dictated words act as vi commands on
# the line. Nothing runs until Enter, but stop dictation before pressing Esc.

[[ $- == *i* ]] || return 0

VOICEKEY_PROMPT_MARK=${VOICEKEY_PROMPT_MARK:-"❯ "}
VOICEKEY_PYTHON=${VOICEKEY_PYTHON:-$HOME/.local/share/voicekey/venv/bin/python}
__voicekey_session="${XDG_RUNTIME_DIR:-/run/user/$UID}/voicekey/shell-session"

if [[ -n ${GHOSTTY_SHELL_FEATURES-} ]]; then
    GHOSTTY_SHELL_FEATURES=${GHOSTTY_SHELL_FEATURES//title/}
fi

__voicekey_title() {
    printf '\e]2;%s\a' "$1"
}

__voicekey_prompt_title() {
    __voicekey_title "$VOICEKEY_PROMPT_MARK${PWD/#"$HOME"/\~}"
}

# Prompt frameworks such as starship rebuild PS1 before each prompt, so keep
# re-adding the (idempotent) prefix after them.
__voicekey_ps1='\[$(__voicekey_prompt_title)\]'
__voicekey_prompt() {
    [[ $PS1 == "$__voicekey_ps1"* ]] || PS1=$__voicekey_ps1$PS1
}

# Runs from PS0: after Enter, before the command.
__voicekey_running() {
    local command
    command=$(HISTTIMEFORMAT= history 1)
    command=${command#*[0-9]  }
    __voicekey_title "${command:0:80}"
    [[ -f $__voicekey_session ]] || return 0
    rm -f -- "$__voicekey_session"
    "$VOICEKEY_PYTHON" -m voicekey --control stop >/dev/null 2>&1 || return 0
    local waited=0
    while ((waited++ < 50)) &&
        "$VOICEKEY_PYTHON" -m voicekey --control status 2>/dev/null | grep -q '"state": "\(listening\|finishing\)"'; do
        sleep 0.1
    done
    # Input-method text committed after Enter is queued for the terminal.
    while IFS= read -r -s -t 0.05 -n 65536 _ </dev/tty 2>/dev/null; do :; done
}

PS0="${PS0-}"'$(__voicekey_running)'
if [[ $(declare -p PROMPT_COMMAND 2>/dev/null) == "declare -a"* ]]; then
    PROMPT_COMMAND+=(__voicekey_prompt)
else
    PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND; }__voicekey_prompt"
fi
