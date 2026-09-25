"""Voicekey's own emacsclient calls must never reach the user's Emacs server.

Point them at a socket that does not exist (and never start an alternate
editor). Tests that need Emacs run a private server and name it with -s.
"""
import os
import tempfile

os.environ["EMACS_SOCKET_NAME"] = os.path.join(tempfile.gettempdir(), "voicekey-tests-no-emacs-server")
os.environ["ALTERNATE_EDITOR"] = "false"
