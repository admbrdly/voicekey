;;; voicekey.el --- Dictation transactions -*- lexical-binding: t; -*-
;; This library installs no hooks unless voicekey-tracking-mode is enabled.
(require 'cl-lib)
(require 'json)
(require 'seq)
(require 'subr-x)

(defconst voicekey--protocol-version 4)
(defvar voicekey--pins nil)
(defvar voicekey--operations nil)
(defvar voicekey--user-buffer nil)
(defvar voicekey--user-marker nil)

(defun voicekey--remember-user-position ()
  "Remember command-loop position; server evaluations do not run this hook."
  (setq voicekey--user-buffer (window-buffer (selected-window)))
  (with-current-buffer voicekey--user-buffer
    (if (markerp voicekey--user-marker)
        (set-marker voicekey--user-marker (point) (current-buffer))
      (setq voicekey--user-marker (copy-marker (point) t)))))

(define-minor-mode voicekey-tracking-mode
  "Track the user's command-loop buffer for dictation binding.
Enable explicitly in your configuration. Loading the library alone does not
alter command-loop behavior. This is the anchor groundwork for persistent mode."
  :global t
  (if voicekey-tracking-mode
      (progn
        (voicekey--remember-user-position)
        (add-hook 'post-command-hook #'voicekey--remember-user-position))
    (remove-hook 'post-command-hook #'voicekey--remember-user-position)
    (when (markerp voicekey--user-marker) (set-marker voicekey--user-marker nil))
    (setq voicekey--user-buffer nil voicekey--user-marker nil)))

(defun voicekey--state ()
  (if (bound-and-true-p evil-local-mode) evil-state 'none))

(defun voicekey--insertion-position ()
  (pcase (voicekey--state)
    ('visual evil-visual-beginning)
    ('normal (min (1+ (point)) (line-end-position)))
    (_ (point))))

(defun voicekey--identity ()
  "Name the current buffer and its major mode for refusals."
  (format "%s (%s)" (buffer-name) major-mode))

(defun voicekey--pin (id expires &optional pid)
  "Bind a buffer only when this request is handled before EXPIRES.
PID is the process owning the focused window when the compositor reports
it. `emacsclient' reaches one server, so a window of another Emacs process
is refused instead of being bound to whatever this server has selected.
A successful pin answers with a JSON description of the bound buffer."
  (cond
   ((> (float-time) expires) "refused: buffer binding expired")
   ((and pid (/= pid (emacs-pid)))
    (format "refused: the focused window belongs to Emacs process %d, not to this server (process %d)"
            pid (emacs-pid)))
   (t
    (let ((buffer (if (and (bound-and-true-p voicekey-tracking-mode)
                           (buffer-live-p voicekey--user-buffer))
                      voicekey--user-buffer
                    (window-buffer (selected-window)))))
      (setq voicekey--pins
            (cons (list id buffer)
                  (seq-take (assoc-delete-all id voicekey--pins) 15)))
      (with-current-buffer buffer
        (let ((pos (voicekey--insertion-position)))
          (json-encode
           (list (cons "before" (if (> pos (point-min)) (string (char-before pos)) ""))
                 (cons "buffer" (buffer-name))
                 (cons "mode" (symbol-name major-mode))
                 (cons "read_only" (if buffer-read-only t :json-false))
                 (cons "state" (symbol-name (voicekey--state)))
                 (cons "pid" (emacs-pid))))))))))

(defun voicekey--spaced (text pos)
  (if (or (string-empty-p text)
          (string-match-p "\\`[[:space:]]" text)
          (memq (aref text 0) '(?, ?. ?\; ?: ?! ?? ?\) ?\] ?\}))
          (<= pos (point-min))
          (memq (char-before pos) '(?\s ?\t ?\n ?\( ?\[ ?\{ ?\" ?\' ?“ ?‘)))
      text
    (concat " " text)))

(defun voicekey--unpin (id)
  (setq voicekey--pins (assoc-delete-all id voicekey--pins))
  "ok")

(defun voicekey--prepare-text (text terminal)
  "Preserve buffer formatting, flatten terminal input, refuse other controls.
Keep this policy in step with delivery.prepare in the Python backends.
Run inside the insertion transaction: a buffer can change mode after pinning."
  (setq text (replace-regexp-in-string "\r\n\\|[\r\013\014\u0085\u2028\u2029]" "\n" text t t))
  (when (string-match "[\u0000-\u0008\u000b-\u001f\u007f-\u009f]" text)
    (error "dictation contains control character U+%04X"
           (aref text (match-beginning 0))))
  (if terminal
      (replace-regexp-in-string
       "[ \t\n]+"
       (lambda (whitespace)
         (if (string-match-p "[\t\n]" whitespace) " " whitespace))
       text t t)
    text))

(defun voicekey--insert (id operation expires text fallback &optional permit keep-pin)
  "Insert once for OPERATION; refuse expiry before any mutation.
An error after mutation begins is reported as unknown. Buffer text changes
are grouped atomically; terminal writes cannot be rolled back."
  (let ((previous (assoc operation voicekey--operations)))
    (cond
     (previous (cadr previous))
     ((> (float-time) expires) "refused: insertion expired")
     ((and permit (not (file-exists-p permit))) "refused: insertion cancelled")
     (t
      (let* ((buffer (cadr (assoc id voicekey--pins)))
             (started nil)
             (answer
              (catch 'answer
                (unless (buffer-live-p buffer) (throw 'answer "refused: the pinned buffer is gone"))
                (condition-case err
                    (with-selected-window (or (get-buffer-window buffer t) (selected-window))
                      (with-current-buffer buffer
                        (let* ((state (voicekey--state))
                               (terminal (memq major-mode '(vterm-mode term-mode)))
                               (pos (voicekey--insertion-position)))
                          (when (and buffer-read-only (not terminal))
                            (throw 'answer (format "refused: buffer is read-only: %s"
                                                   (voicekey--identity))))
                          (when (eq state 'operator)
                            (throw 'answer "refused: an operator is pending"))
                          (when (and (eq state 'visual) (eq (evil-visual-type) 'block))
                            (throw 'answer "refused: blockwise selection"))
                          (when (or (> (float-time) expires)
                                    (and permit (not (file-exists-p permit))))
                            (throw 'answer "refused: insertion expired"))
                          (setq text (voicekey--prepare-text
                                      (if terminal (concat fallback text) text) terminal))
                          (setq started t)
                          (if terminal
                              (if (eq major-mode 'vterm-mode)
                                  (vterm-send-string text)
                                (term-send-raw-string text))
                            (setq text (voicekey--spaced text pos))
                            (undo-boundary)
                            (atomic-change-group
                              (cond
                               ((eq state 'visual)
                                (evil-change evil-visual-beginning evil-visual-end (evil-visual-type))
                                (insert text)
                                (evil-normal-state))
                               ((eq state 'normal)
                                (evil-append 1) (insert text) (evil-normal-state))
                               (t (insert text))))
                            (undo-boundary))
                          "ok")))
                  (error (concat (if started "unknown: " "refused: ")
                                 (error-message-string err)))))))
        (unless keep-pin (voicekey--unpin id))
        (setq voicekey--operations
              (cons (list operation answer expires)
                    (seq-take (seq-filter (lambda (item) (> (nth 2 item) (float-time)))
                                          voicekey--operations) 255)))
        answer)))))

(provide 'voicekey)
;;; voicekey.el ends here
