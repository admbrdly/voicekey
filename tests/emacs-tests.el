;;; Tests use only a separate batch Emacs, never the user's server.
(require 'ert)
(require 'cl-lib)
(require 'json)
(require 'voicekey)

(defmacro voicekey-test-buffer (text &rest body)
  `(let ((voicekey--pins nil) (voicekey--operations nil)
         (voicekey-tracking-mode nil) (use-dialog-box nil))
     (cl-letf (((symbol-function 'y-or-n-p) (lambda (&rest _) (error "would prompt")))
               ((symbol-function 'yes-or-no-p) (lambda (&rest _) (error "would prompt"))))
       (save-window-excursion
         (with-temp-buffer
           (set-window-buffer (selected-window) (current-buffer))
           (insert ,text)
           ,@body)))))

(defun voicekey-test-expiry () (+ (float-time) 60))
(defun voicekey-test-pin () (voicekey--pin "pin" (voicekey-test-expiry)))
(defun voicekey-test-ack () (json-read-from-string (voicekey-test-pin)))
(defun voicekey-test-before () (cdr (assq 'before (voicekey-test-ack))))
(defun voicekey-test-insert (text &optional operation)
  (voicekey--insert "pin" (or operation "operation") (voicekey-test-expiry) text ""))

(ert-deftest voicekey-draft-is-virtual-and-accepts-at-its-anchor ()
  (voicekey-test-buffer "first\nsecond"
    (buffer-enable-undo)
    (goto-char 6)
    (voicekey-test-pin)
    (set-buffer-modified-p nil)
    (let ((original-undo buffer-undo-list))
      (should (equal (voicekey--draft "pin" (voicekey-test-expiry) "a draft\nwith two lines") "ok"))
      (should-not (buffer-modified-p))
      (should (eq buffer-undo-list original-undo))
      (should (equal (buffer-string) "first\nsecond")))
    (goto-char (point-min))
    (insert "Before ")
    (goto-char (point-max))
    (let ((position (point)))
      (should (equal (voicekey--draft "pin" (voicekey-test-expiry) "revised") "ok"))
      (should (= (point) position)))
    (should (equal (voicekey-test-insert "accepted") "ok"))
    (should (equal (buffer-string) "Before first accepted\nsecond"))
    (should-not (overlays-in (point-min) (point-max)))))

(ert-deftest voicekey-draft-cancellation-only-removes-its-overlay ()
  (voicekey-test-buffer "unchanged"
    (voicekey-test-pin)
    (should (equal (voicekey--draft "pin" (voicekey-test-expiry) "discard me") "ok"))
    (let ((overlay (nth 3 (assoc "pin" voicekey--pins))))
      (should (overlay-buffer overlay))
      (should (equal (voicekey--unpin "pin") "ok"))
      (should-not (overlay-buffer overlay)))
    (should (equal (buffer-string) "unchanged"))))

(ert-deftest voicekey-draft-cursor-at-anchor-follows-inserted-text ()
  (voicekey-test-buffer "before"
    (voicekey-test-pin)
    (voicekey--draft "pin" (voicekey-test-expiry) "draft")
    (should (equal (voicekey-test-insert "accepted") "ok"))
    (should (equal (buffer-string) "before accepted"))
    (should (= (point) (point-max)))))

(ert-deftest voicekey-draft-acceptance-ignores-later-evil-operator-or-block-selection ()
  (skip-unless (require 'evil nil t))
  (dolist (state '(operator block))
    (voicekey-test-buffer "old text"
      (evil-local-mode 1)
      (evil-normal-state)
      (goto-char 3)
      (voicekey-test-pin)
      (should (equal (voicekey--draft "pin" (voicekey-test-expiry) "draft") "ok"))
      (if (eq state 'operator)
          (evil-operator-state)
        (evil-visual-select 1 3 'block))
      (should (equal (voicekey-test-insert "accepted") "ok"))
      (should (equal (buffer-string) "old accepted text")))))

(ert-deftest voicekey-new-draft-clears-an-abandoned-preview ()
  (voicekey-test-buffer "unchanged"
    (voicekey-test-pin)
    (voicekey--draft "pin" (voicekey-test-expiry) "abandoned")
    (let ((old (nth 3 (assoc "pin" voicekey--pins))))
      (voicekey--pin "new" (voicekey-test-expiry))
      (voicekey--draft "new" (voicekey-test-expiry) "fresh")
      (should-not (overlay-buffer old))
      (should-not (assoc "pin" voicekey--pins))
      (voicekey--unpin "new"))
    (should (equal (buffer-string) "unchanged"))))

(ert-deftest voicekey-draft-refuses-expiry-read-only-and-terminal-buffers ()
  (voicekey-test-buffer "old"
    (voicekey-test-pin)
    (should (string-prefix-p "refused:" (voicekey--draft "pin" 0 "late")))
    (let ((buffer-read-only t))
      (should (string-prefix-p "refused:" (voicekey--draft "pin" (voicekey-test-expiry) "new"))))
    (let ((major-mode 'vterm-mode))
      (should (string-prefix-p "refused:" (voicekey--draft "pin" (voicekey-test-expiry) "new"))))
    (should-not (nth 2 (assoc "pin" voicekey--pins)))
    (should (equal (buffer-string) "old"))))

(ert-deftest voicekey-refused-draft-keeps-ordinary-terminal-delivery ()
  (dolist (mode '(term-mode vterm-mode))
    (voicekey-test-buffer "old"
      (voicekey-test-pin)
      (let ((major-mode mode) (buffer-read-only t) sent)
        (cl-letf (((symbol-function 'term-send-raw-string) (lambda (text) (setq sent text)))
                  ((symbol-function 'vterm-send-string) (lambda (text) (setq sent text))))
          (should (equal (voicekey--draft "pin" (voicekey-test-expiry) "")
                         "refused: drafts require an editable text buffer"))
          (should-not (nth 2 (assoc "pin" voicekey--pins)))
          (should (equal (voicekey-test-insert "words") "ok"))
          (should (equal sent "words"))
          (should (equal (buffer-string) "old")))))))

(ert-deftest voicekey-refused-draft-keeps-read-only-protection ()
  (voicekey-test-buffer "old"
    (voicekey-test-pin)
    (let ((buffer-read-only t))
      (should (equal (voicekey--draft "pin" (voicekey-test-expiry) "")
                     "refused: drafts require an editable text buffer"))
      (should-not (nth 2 (assoc "pin" voicekey--pins)))
      (should (string-prefix-p "refused: buffer is read-only" (voicekey-test-insert "words")))
      (should (equal (buffer-string) "old")))))

(ert-deftest voicekey-refused-minibuffer-draft-keeps-ordinary-insertion ()
  (voicekey-test-buffer "old"
    (voicekey-test-pin)
    (cl-letf (((symbol-function 'minibufferp) (lambda (&rest _) t)))
      (should (equal (voicekey--draft "pin" (voicekey-test-expiry) "")
                     "refused: drafts require an editable text buffer")))
    (should-not (nth 2 (assoc "pin" voicekey--pins)))
    (should (equal (voicekey-test-insert "words") "ok"))
    (should (equal (buffer-string) "old words"))))

(ert-deftest voicekey-draft-preview-preserves-the-users-window-buffer-and-point ()
  (voicekey-test-buffer "source"
    (voicekey-test-pin)
    (let ((source (current-buffer)))
      (with-temp-buffer
        (set-window-buffer (selected-window) (current-buffer))
        (insert "user edits")
        (let ((buffer (current-buffer)) (window (selected-window)) (position (point)))
          (should (equal (voicekey--draft "pin" (voicekey-test-expiry) "background draft") "ok"))
          (should (eq (current-buffer) buffer))
          (should (eq (selected-window) window))
          (should (= (point) position))
          (should (equal (buffer-string) "user edits"))))
      (voicekey--unpin "pin")
      (should (equal (with-current-buffer source (buffer-string)) "source")))))

(ert-deftest voicekey-persistent-buffer-follows-point-and-inserts-in-background ()
  (voicekey-test-buffer "section 2\nsection 5\n"
    (goto-char 10)
    (voicekey-test-pin)
    (let ((original (current-buffer)))
      (should (equal (voicekey--insert "pin" "one" (voicekey-test-expiry) "first" "" nil t) "ok"))
      (with-temp-buffer
        (set-window-buffer (selected-window) (current-buffer))
        (insert "PDF")
        (let ((pdf (current-buffer)) (position (point)) (window (selected-window)))
          (should (equal (voicekey--insert "pin" "two" (voicekey-test-expiry) "background" "" nil t) "ok"))
          (should (eq (current-buffer) pdf))
          (should (eq (selected-window) window))
          (should (= (point) position))
          (should (equal (buffer-string) "PDF"))))
      (set-window-buffer (selected-window) original)
      (goto-char (point-min))
      (forward-line 1)
      (end-of-line)
      (should (equal (voicekey--insert "pin" "three" (voicekey-test-expiry) "new section" "" nil t) "ok"))
      (should (equal (buffer-string) "section 2 first background\nsection 5 new section\n"))
      (should (assoc "pin" voicekey--pins))
      (voicekey--unpin "pin")
      (should-not (assoc "pin" voicekey--pins))
      (should (string-prefix-p "refused:" (voicekey--insert "pin" "late" (voicekey-test-expiry) "late" "" nil t))))))

(ert-deftest voicekey-persistent-operations-remain-idempotent-with-live-pin ()
  (voicekey-test-buffer "old"
    (voicekey-test-pin)
    (dotimes (_ 2)
      (should (equal (voicekey--insert "pin" "one" (voicekey-test-expiry) "next" "" nil t) "ok")))
    (should (assoc "pin" voicekey--pins))
    (should (equal (buffer-string) "old next"))))

(ert-deftest voicekey-insert-spacing-and-punctuation ()
  (voicekey-test-buffer "old"
    (voicekey-test-pin)
    (should (equal (voicekey-test-insert "next") "ok"))
    (should (equal (buffer-string) "old next"))
    (voicekey-test-pin)
    (should (equal (voicekey-test-insert ", please" "second") "ok"))
    (should (equal (buffer-string) "old next, please"))))

(ert-deftest voicekey-pin-and-insert-expiry-do-nothing ()
  (voicekey-test-buffer "old"
    (should (string-prefix-p "refused:" (voicekey--pin "late" 0)))
    (should-not voicekey--pins)
    (voicekey-test-pin)
    (should (string-prefix-p "refused:" (voicekey--insert "pin" "late" 0 "new" "")))
    (should (equal (buffer-string) "old"))))

(ert-deftest voicekey-revoked-permission-does-nothing ()
  (voicekey-test-buffer "old"
    (voicekey-test-pin)
    (should (string-prefix-p "refused:"
             (voicekey--insert "pin" "late" (voicekey-test-expiry) "new" "" "/nonexistent/voicekey-permit")))
    (should (equal (buffer-string) "old"))))

(ert-deftest voicekey-duplicate-operation-is-idempotent ()
  (voicekey-test-buffer "old"
    (voicekey-test-pin)
    (should (equal (voicekey-test-insert "next") "ok"))
    (should (equal (voicekey-test-insert "next") "ok"))
    (should (equal (buffer-string) "old next"))))

(ert-deftest voicekey-readonly-is-a-definite-refusal-naming-the-buffer ()
  (voicekey-test-buffer "old"
    (voicekey-test-pin)
    (setq buffer-read-only t)
    (let ((answer (voicekey-test-insert "next")))
      (should (string-prefix-p "refused: buffer is read-only" answer))
      (should (string-match-p (regexp-quote (buffer-name)) answer))
      (should (string-match-p (symbol-name major-mode) answer)))
    (should (equal (buffer-string) "old"))))

(ert-deftest voicekey-pin-describes-the-bound-buffer ()
  (voicekey-test-buffer "old"
    (let ((ack (voicekey-test-ack)))
      (should (equal (cdr (assq 'before ack)) "d"))
      (should (equal (cdr (assq 'buffer ack)) (buffer-name)))
      (should (equal (cdr (assq 'mode ack)) (symbol-name major-mode)))
      (should (eq (cdr (assq 'read_only ack)) :json-false))
      (should (equal (cdr (assq 'state ack)) "none"))
      (should (= (cdr (assq 'pid ack)) (emacs-pid))))
    (setq buffer-read-only t)
    (should (eq (cdr (assq 'read_only (voicekey-test-ack))) t))
    (should (assoc "pin" voicekey--pins))))

(ert-deftest voicekey-pin-for-another-emacs-process-is-refused ()
  (voicekey-test-buffer "old"
    (let ((answer (voicekey--pin "pin" (voicekey-test-expiry) (1+ (emacs-pid)))))
      (should (string-prefix-p "refused:" answer))
      (should (string-match-p "not to this server" answer)))
    (should-not voicekey--pins)
    (should (string-prefix-p "refused:" (voicekey-test-insert "next")))
    (should (equal (buffer-string) "old"))
    (should (equal (cdr (assq 'buffer (json-read-from-string
                                        (voicekey--pin "pin" (voicekey-test-expiry) (emacs-pid)))))
                   (buffer-name)))
    (should (equal (voicekey-test-insert "next" "own") "ok"))
    (should (equal (buffer-string) "old next"))))

(ert-deftest voicekey-mutation-hook-error-is-unknown-and-text-rolls-back ()
  (voicekey-test-buffer "old"
    (voicekey-test-pin)
    (let ((after-change-functions (list (lambda (&rest _) (error "editing hook failed")))))
      (should (string-prefix-p "unknown:" (voicekey-test-insert "next"))))
    (should (equal (buffer-string) "old"))))

(ert-deftest voicekey-pin-does-not-follow-another-buffer ()
  (voicekey-test-buffer "first"
    (let ((original (current-buffer)))
      (voicekey-test-pin)
      (with-temp-buffer
        (set-window-buffer (selected-window) (current-buffer))
        (insert "second")
        (let ((selected (current-buffer)))
          (should (equal (voicekey-test-insert "next") "ok"))
          (should (eq (current-buffer) selected)))
        (should (equal (buffer-string) "second")))
      (should (equal (with-current-buffer original (buffer-string)) "first next")))))

(ert-deftest voicekey-killed-buffer-is-refused ()
  (let ((voicekey--pins nil))
    (let ((buffer (generate-new-buffer " *voicekey-test*")))
      (setq voicekey--pins (list (list "pin" buffer)))
      (kill-buffer buffer)
      (should (string-prefix-p "refused:" (voicekey-test-insert "next" "killed"))))))

(ert-deftest voicekey-narrowing-is-respected ()
  (voicekey-test-buffer "before\nallowed\nafter"
    (narrow-to-region 8 15)
    (goto-char (point-max))
    (voicekey-test-pin)
    (should (equal (voicekey-test-insert "next") "ok"))
    (widen)
    (should (equal (buffer-string) "before\nallowed next\nafter"))))

(ert-deftest voicekey-terminal-errors-are-unknown ()
  (voicekey-test-buffer ""
    (setq major-mode 'vterm-mode)
    (voicekey-test-pin)
    (cl-letf (((symbol-function 'vterm-send-string) (lambda (_) (error "partial terminal send"))))
      (should (string-prefix-p "unknown:" (voicekey-test-insert "next"))))))

(ert-deftest voicekey-ordinary-buffer-preserves-paragraphs-and-tabs ()
  (voicekey-test-buffer ""
    (voicekey-test-pin)
    (should (equal (voicekey-test-insert "first\r\n\r\n\tsecond") "ok"))
    (should (equal (buffer-string) "first\n\n\tsecond"))))

(ert-deftest voicekey-terminal-formatting-is-filtered-even-after-mode-change ()
  (dolist (mode '(vterm-mode term-mode))
    (voicekey-test-buffer ""
      (voicekey-test-pin)
      ;; Classification must happen during insertion, not when pinned.
      (setq major-mode mode)
      (let (sent)
        (cl-letf (((symbol-function 'vterm-send-string) (lambda (text) (push text sent)))
                  ((symbol-function 'term-send-raw-string) (lambda (text) (push text sent))))
          (should (equal (voicekey-test-insert "first\r\n\tsecond\u2028last\n") "ok"))
          (should (equal sent '("first second last ")))
          (should (equal (buffer-string) "")))))))

(ert-deftest voicekey-controls-refuse-before-any-buffer-or-terminal-mutation ()
  (dolist (mode '(fundamental-mode vterm-mode term-mode))
    (voicekey-test-buffer "old"
      (setq major-mode mode)
      (voicekey-test-pin)
      (cl-letf (((symbol-function 'vterm-send-string) (lambda (_) (ert-fail "terminal write")))
                ((symbol-function 'term-send-raw-string) (lambda (_) (ert-fail "terminal write"))))
        (should (string-prefix-p "refused: dictation contains control character U+001B"
                                  (voicekey-test-insert "before\eafter")))
        (should (equal (buffer-string) "old"))))))

(ert-deftest voicekey-preparation-covers-all-control-characters ()
  (dolist (code (append (number-sequence 0 31) (number-sequence 127 159)))
    (unless (memq code '(9 10 11 12 13 133))
      (should-error (voicekey--prepare-text (string code) nil))
      (should-error (voicekey--prepare-text (string code) t))))
  (dolist (separator '("\n" "\r" "\r\n" "\013" "\014" "\u0085" "\u2028" "\u2029" "\t"))
    (should (equal (voicekey--prepare-text (concat "one" separator "two") t) "one two"))))

(ert-deftest voicekey-flattening-absorbs-only-spaces-next-to-formatting ()
  (should (equal (voicekey--prepare-text "one  \r\n\n  \t  two" t) "one two"))
  (should (equal (voicekey--prepare-text "one  two   " t) "one  two   "))
  (should (equal (voicekey--prepare-text "one \n  two" nil) "one \n  two")))

(ert-deftest voicekey-evil-normal-spaces-after-character-at-point ()
  (skip-unless (require 'evil nil t))
  (voicekey-test-buffer "a b"
    (evil-local-mode 1)
    (evil-normal-state)
    (goto-char 3)
    (should (equal (voicekey-test-before) "b"))
    (should (equal (voicekey-test-insert "next") "ok"))
    (should (equal (buffer-string) "a b next"))
    (should (eq evil-state 'normal))))

(ert-deftest voicekey-evil-insert-stays-insert ()
  (skip-unless (require 'evil nil t))
  (voicekey-test-buffer "old"
    (evil-local-mode 1)
    (evil-insert-state)
    (goto-char (point-max))
    (voicekey-test-pin)
    (should (equal (voicekey-test-insert "next") "ok"))
    (should (eq evil-state 'insert))
    (should (equal (buffer-string) "old next"))))

(ert-deftest voicekey-evil-visual-replaces-selection-and-restores-normal ()
  (skip-unless (require 'evil nil t))
  (voicekey-test-buffer "old text"
    (evil-local-mode 1)
    ;; This API takes a range type (inclusive), not a selection name (char).
    (evil-visual-select 1 4 evil-visual-char)
    (should (eq (evil-visual-type) evil-visual-char))
    (voicekey-test-pin)
    (should (equal (voicekey-test-insert "new") "ok"))
    (should (equal (buffer-string) "new text"))
    (should (eq evil-state 'normal))))

(ert-deftest voicekey-evil-operator-and-block-selection-are-refused ()
  (skip-unless (require 'evil nil t))
  (voicekey-test-buffer "old text"
    (evil-local-mode 1)
    (evil-operator-state)
    (voicekey-test-pin)
    (should (string-prefix-p "refused:" (voicekey-test-insert "new")))
    (evil-visual-select 1 3 'block)
    (voicekey-test-pin)
    (should (string-prefix-p "refused:" (voicekey-test-insert "new" "block")))
    (should (equal (buffer-string) "old text"))))

(ert-deftest voicekey-marker-spike-retains-anchor-and-insertion-order ()
  (voicekey-test-buffer "old"
    (let ((first (copy-marker (point) t))
          (second (copy-marker (point) t)))
      (goto-char (point-min))
      (save-excursion (goto-char first) (insert " first"))
      (save-excursion (goto-char second) (insert " second"))
      (should (equal (buffer-string) "old first second"))
      (should (= (point) (point-min))))))
