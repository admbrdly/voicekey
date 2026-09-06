;;; Tests use only a separate batch Emacs, never the user's server.
(require 'ert)
(require 'cl-lib)
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
(defun voicekey-test-insert (text &optional operation)
  (voicekey--insert "pin" (or operation "operation") (voicekey-test-expiry) text ""))

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

(ert-deftest voicekey-readonly-is-a-definite-refusal ()
  (voicekey-test-buffer "old"
    (voicekey-test-pin)
    (setq buffer-read-only t)
    (should (string-prefix-p "refused:" (voicekey-test-insert "next")))
    (should (equal (buffer-string) "old"))))

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

(ert-deftest voicekey-evil-normal-spaces-after-character-at-point ()
  (skip-unless (require 'evil nil t))
  (voicekey-test-buffer "a b"
    (evil-local-mode 1)
    (evil-normal-state)
    (goto-char 3)
    (should (equal (voicekey-test-pin) "b"))
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
