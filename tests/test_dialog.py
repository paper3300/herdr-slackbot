"""Dialog parser on the synthetic copies of screens captured from Herdr 0.8.2 (Claude Code 2.1.285,
Codex 0.159.0) while the agent was `blocked`:

- dialog_claude_permission: Bash permission prompt; option 2 wraps onto a second line.
- dialog_claude_trust: Claude's folder-trust screen at startup (unnumbered `❯ No, exit` menu).
- dialog_ask_single / dialog_ask_multi / dialog_ask_review: one AskUserQuestion with two questions
  (single-select Color, multi-select Fruits) and its review tab.
- dialog_ask_type: single question with the cursor on "Type something." (ctrl+g footer).
- dialog_ask_boxed: a `☐ <header>` question whose text is `│ `-prefixed over two lines.
- dialog_plan: plan approval (ExitPlanMode) with the plan file in the footer.
- dialog_codex_approval: Codex command approval (no rule above the dialog, `›` cursor).
- dialog_codex_trust: Codex folder trust at startup (Herdr reports it `idle`).
"""

import pytest

from herdr_slackbot.dialog import (
    KIND_CODEX_APPROVAL, KIND_PERMISSION, KIND_PLAN, KIND_QUESTION, KIND_QUESTION_REVIEW, KIND_TRUST,
    KIND_UNKNOWN, Option, keys_for, parse_dialog, screen_fingerprint, tail_lines,
)


def parse(transcript, name, kind="claude"):
    return parse_dialog(transcript(name), kind)


def labels(d):
    return [o.label for o in d.options]


def test_claude_permission(transcript):
    d = parse(transcript, "dialog_claude_permission.txt")
    assert d.kind == KIND_PERMISSION
    assert d.title == "Bash command"
    assert d.body == ("echo spike > a.txt", "Write 'spike' to a.txt")
    assert d.question == "Do you want to proceed?"
    assert [o.number for o in d.options] == [1, 2, 3]
    assert d.options[0].label == "Yes" and d.options[2].label == "No"
    assert d.options[1].label.startswith("Yes, and always allow access to D:\\work\\demo\\")
    assert d.options[1].label.endswith(" from this project")  # the wrapped line joins the label
    assert d.options[1].description == ""
    assert [o.cursor for o in d.options] == [True, False, False]
    assert not d.multi_select and d.tabs == () and d.plan_file is None
    assert [keys_for(d, i) for i in range(3)] == [["1"], ["2"], ["3"]]


def test_existing_blocked_fixture_is_a_permission_dialog(transcript):
    d = parse(transcript, "claude_blocked_visible.txt")
    assert d.kind == KIND_PERMISSION and d.title == "Bash command" and len(d.options) == 3


def test_claude_trust_unnumbered_menu(transcript):
    d = parse(transcript, "dialog_claude_trust.txt")
    assert d.kind == KIND_TRUST
    assert d.title == "Accessing workspace:"
    assert d.body[0].startswith("D:\\work\\demo\\")
    assert any("Quick safety check" in line for line in d.body)
    assert labels(d) == ["No, exit", "Yes, I trust this folder"]
    assert [o.number for o in d.options] == [None, None]
    assert d.options[0].cursor
    assert keys_for(d, 0) == ["enter"]
    assert keys_for(d, d.options[1]) == ["down", "enter"]


def test_unnumbered_menu_moves_up_from_the_cursor(transcript):
    screen = transcript("dialog_claude_trust.txt").replace(" ❯ No, exit", "   No, exit").replace(
        "   Yes, I trust this folder", " ❯ Yes, I trust this folder")
    d = parse_dialog(screen, "claude")
    assert d.options[1].cursor
    assert keys_for(d, 0) == ["up", "enter"]
    assert keys_for(d, 1) == ["enter"]


def test_ask_single_select_with_tabs(transcript):
    d = parse(transcript, "dialog_ask_single.txt")
    assert d.kind == KIND_QUESTION
    assert d.tabs == (("Color", False), ("Fruits", False))
    assert d.title == "Color" and d.question == "Pick a color?"
    assert labels(d) == ["Red", "Blue", "Green", "Type something.", "Chat about this"]
    assert d.options[0].description == "A warm, vibrant color associated with energy and passion"
    assert [o.free_text for o in d.options] == [False, False, False, True, False]
    assert [o.chat for o in d.options] == [False, False, False, False, True]
    assert all(o.checked is None for o in d.options) and not d.multi_select
    assert keys_for(d, 4) == ["5"]


def test_ask_multi_select(transcript):
    d = parse(transcript, "dialog_ask_multi.txt")
    assert d.kind == KIND_QUESTION and d.multi_select
    assert d.tabs == (("Color", True), ("Fruits", False))
    assert d.title == "Fruits" and d.question == "Pick fruits?"
    assert labels(d) == ["Apple", "Banana", "Cherry", "Type something", "Chat about this"]
    assert [o.checked for o in d.options] == [False, False, False, False, None]
    assert d.options[1].description == "A soft, creamy fruit packed with potassium"
    assert d.options[3].free_text and d.options[3].description == ""  # the `Submit` line is not part of it


def test_ask_review(transcript):
    d = parse(transcript, "dialog_ask_review.txt")
    assert d.kind == KIND_QUESTION_REVIEW
    assert d.tabs == (("Color", True), ("Fruits", True))
    assert d.title == "Review your answers"
    assert d.body == ("● Pick a color?", "  → Blue", "● Pick fruits?", "  → Apple, Cherry")
    assert d.question == "Ready to submit your answers?"
    assert labels(d) == ["Submit answers", "Cancel"]


def test_ask_type_something_with_cursor(transcript):
    d = parse(transcript, "dialog_ask_type.txt")
    assert d.kind == KIND_QUESTION
    assert d.tabs == (("Name", False),) and d.title == "Name" and d.question == "Pick a name?"
    assert labels(d) == ["Alpha", "Beta", "Type something.", "Chat about this"]
    assert d.options[2].cursor and d.options[2].free_text
    assert d.free_text_option() is d.options[2]


def test_ask_boxed_question(transcript):
    d = parse(transcript, "dialog_ask_boxed.txt")
    assert d.kind == KIND_QUESTION
    assert d.tabs == (("Storage", False),) and d.title == "Storage"
    assert d.question.startswith("The demo service currently keeps everything in memory.")
    assert d.question.endswith("changes the setup steps in the README.")
    assert "│" not in d.question
    assert labels(d) == ["SQLite file", "PostgreSQL", "Keep it in memory", "Type something.", "Chat about this"]
    assert d.options[2].description.startswith("No persistence.")
    assert d.options[3].free_text and d.options[4].chat


def test_plan_approval(transcript):
    d = parse(transcript, "dialog_plan.txt")
    assert d.kind == KIND_PLAN
    assert d.title == "Ready to code?"
    assert d.body == ('1. Create b.txt in the working directory with the content "plan".',
                      "2. Verify by reading b.txt back.")
    assert d.question == "Claude has written up a plan and is ready to execute. Would you like to proceed?"
    assert labels(d) == ["Yes, auto-accept edits", "Yes, manually approve edits", "Tell Claude what to change"]
    assert d.options[2].free_text
    assert d.options[2].description == "shift+tab to approve with this feedback"
    assert d.plan_file == "~\\.claude\\plans\\plan-create-b-txt-demo-slug-for-tests-abcdef.md"


def test_codex_approval(transcript):
    d = parse(transcript, "dialog_codex_approval.txt", "codex")
    assert d.kind == KIND_CODEX_APPROVAL
    assert d.question == "Would you like to run the following command?"
    assert "$ Set-Content -LiteralPath 'e.txt' -Value 'codex2' -NoNewline" in d.body
    assert d.body[0] == "Environment: local"
    assert labels(d)[0] == "Yes, proceed (y)"
    assert labels(d)[2] == "No, and tell Codex what to do differently (esc)"
    assert not any(o.free_text for o in d.options)  # a plain option for Codex
    assert d.options[0].cursor
    assert "Update available" not in " ".join(d.body)  # the banner above the blank run is not part of it


def test_codex_trust(transcript):
    d = parse(transcript, "dialog_codex_trust.txt", "codex")
    assert d.kind == KIND_TRUST
    assert d.title == "Folder access"
    assert labels(d) == ["Trust and continue", "Back to Agent Command Center"]
    assert keys_for(d, 0) == ["1"]


@pytest.mark.parametrize("name", ["claude_short.txt", "claude_long_multiblock.txt", "claude_capture_gap.txt",
                                  "codex_idle.txt", "codex_answer.txt", "claude_multiturn_sticky_recap.txt"])
def test_screens_without_a_dialog(transcript, name):
    assert parse(transcript, name) is None


def test_empty_screen():
    assert parse_dialog("", "claude") is None
    assert parse_dialog("\n\n  \n", None) is None


def test_unclassified_dialog_keeps_its_options():
    screen = "some output\n\n" + "─" * 40 + "\n Pick one\n ❯ 1. Left\n   2. Right\n\n Esc to cancel\n"
    d = parse_dialog(screen, None)
    assert d.kind == KIND_UNKNOWN
    assert d.title == "Pick one" and labels(d) == ["Left", "Right"]


def test_fingerprint_ignores_cursor_moves(transcript):
    screen = transcript("dialog_ask_single.txt")
    moved = screen.replace("❯ 1. Red", "  1. Red").replace("  2. Blue", "❯ 2. Blue")
    a, b = parse_dialog(screen), parse_dialog(moved)
    assert b.options[1].cursor and not b.options[0].cursor
    assert a.fingerprint == b.fingerprint


def test_fingerprint_ignores_footer_hints(transcript):
    screen = transcript("dialog_ask_type.txt")
    other = screen.replace(" · ctrl+g to edit in Notepad", "")
    assert parse_dialog(screen).fingerprint == parse_dialog(other).fingerprint


def test_fingerprint_changes_with_checkboxes_and_questions(transcript):
    multi = transcript("dialog_ask_multi.txt")
    toggled = multi.replace("1. [ ] Apple", "1. [✔] Apple")
    a, b = parse_dialog(multi), parse_dialog(toggled)
    assert b.options[0].checked is True
    assert a.fingerprint != b.fingerprint
    assert parse(transcript, "dialog_ask_single.txt").fingerprint != a.fingerprint
    assert parse(transcript, "dialog_ask_review.txt").fingerprint != a.fingerprint


def test_fingerprint_changes_with_the_command(transcript):
    screen = transcript("dialog_claude_permission.txt")
    other = screen.replace("   echo spike > a.txt", "   echo other > a.txt")
    assert parse_dialog(screen).fingerprint != parse_dialog(other).fingerprint


def test_keys_for_large_numbers_navigate():
    options = tuple(Option(n, f"o{n}", cursor=(n == 1)) for n in range(1, 12))
    from herdr_slackbot.dialog import Dialog
    d = Dialog("unknown", "", (), "", options)
    assert keys_for(d, 8) == ["9"]
    assert keys_for(d, 10) == ["down"] * 10 + ["enter"]


def test_tail_lines_and_screen_fingerprint():
    screen = "a\n\n b  \nc\n\n"
    assert tail_lines(screen, 2) == [" b", "c"]
    assert screen_fingerprint(screen) == screen_fingerprint("a\n b\nc")
    assert screen_fingerprint(screen) != screen_fingerprint("a\nb\nc")


# --- review round 1 --------------------------------------------------------------------------------

def test_fingerprint_covers_the_plan_text(transcript):
    """M2: the plan sits above the dialog's rule; a different plan is a different dialog."""
    screen = transcript("dialog_plan.txt")
    other = screen.replace("Create b.txt in the working directory", "Delete every file in the repository")
    a, b = parse_dialog(screen), parse_dialog(other)
    assert a.body != b.body and a.fingerprint != b.fingerprint
    moved = screen.replace("❯ 1. Yes, auto-accept edits", "  1. Yes, auto-accept edits").replace(
        "  2. Yes, manually approve edits", "❯ 2. Yes, manually approve edits")
    assert parse_dialog(moved).fingerprint == a.fingerprint  # the cursor still does not count


IDLE_WITH_LIST = "\n".join([
    "● Next steps:",
    "  1. Install deps",
    "  2. Run tests",
    "  3. Deploy",
    "",
    "─" * 60,
    "❯ ",
    "─" * 60,
    "  ? for shortcuts",
])


def test_numbered_list_without_a_cursor_is_not_a_dialog():
    """N2: an idle screen that ends with a list in the answer."""
    assert parse_dialog(IDLE_WITH_LIST, "claude") is None


def test_menu_below_a_numbered_list_wins():
    screen = "\n".join(["Plan:", "  1. Alpha", "  2. Beta", "", "─" * 60, " Continue?", "",
                        " ❯ Stop here", "   Keep going", "", " Enter to confirm"])
    d = parse_dialog(screen, "claude")
    assert [o.label for o in d.options] == ["Stop here", "Keep going"]
    assert d.options[0].number is None


def test_trust_wording_in_an_answer_is_not_the_trust_screen(transcript):
    screen = "\n".join(["• To trust this folder you can:", "", "› 1. Run codex in it", "  2. Accept the prompt",
                        "", "  Press enter to confirm or esc to cancel"])
    d = parse_dialog(screen, "codex")
    assert d is not None and d.kind != KIND_TRUST
    assert parse(transcript, "codex_answer.txt", "codex") is None


def test_multi_select_rows_and_keys(transcript):
    """Live facts: a digit only toggles; the Type something row takes text once the cursor is on
    it (no Enter); Submit = cursor to the Submit row + Enter."""
    from herdr_slackbot.dialog import submit_keys, text_keys
    d = parse(transcript, "dialog_ask_multi.txt")
    assert d.submit_after == 3 and not d.cursor_on_submit
    assert keys_for(d, 0) == ["1"]
    assert text_keys(d, 3) == (["down", "down", "down"], False)
    assert submit_keys(d) == ["down"] * 4 + ["enter"]
    on_type = transcript("dialog_ask_multi.txt").replace("❯ 1. [ ] Apple", "  1. [ ] Apple").replace(
        "  4. [ ] Type something", "❯ 4. [ ] Type something")
    d2 = parse_dialog(on_type)
    assert text_keys(d2, 3) == ([], False) and submit_keys(d2) == ["down", "enter"]
    assert d2.fingerprint == d.fingerprint


def test_multi_select_cursor_on_the_submit_row(transcript):
    from herdr_slackbot.dialog import submit_keys, text_keys
    screen = transcript("dialog_ask_multi.txt").replace("❯ 1. [ ] Apple", "  1. [ ] Apple").replace(
        "     Submit", "❯    Submit")
    d = parse_dialog(screen)
    assert d is not None and d.cursor_on_submit and not any(o.cursor for o in d.options)
    assert submit_keys(d) == ["enter"]
    assert text_keys(d, 3) == (["up"], False)
    assert keys_for(d, 1) == ["2"]


def test_single_select_text_keys_use_the_digit(transcript):
    from herdr_slackbot.dialog import submit_keys, text_keys
    d = parse(transcript, "dialog_ask_single.txt")
    assert text_keys(d, 3) == (["4"], True)
    assert submit_keys(d) is None


# --- review round 2 ---------------------------------------------------------------------------------

def test_multi_select_row_with_typed_multi_line_text(transcript):
    """Live screen (orchestrator): 'hello a<LF>b' typed into the Type something row; the second
    line is drawn at the description indent, then the Submit row."""
    from herdr_slackbot.dialog import submit_keys, text_keys
    d = parse_dialog(transcript("dialog_ask_multi_typed.txt"), "claude", typed_row=2)  # the bridge's evidence
    assert d.kind == KIND_QUESTION and d.multi_select
    assert labels(d) == ["One", "Two", "hello a\nb", "Chat about this"]
    typed = d.options[2]
    assert typed.free_text and typed.checked is True and typed.cursor and typed.description == ""
    assert d.options[0].description == "First option"  # other rows keep their descriptions
    assert d.submit_after == 2 and not d.cursor_on_submit
    assert submit_keys(d) == ["down", "enter"]
    assert text_keys(d, 2) == ([], False)
    other = transcript("dialog_ask_multi_typed.txt").replace("         b\n", "         c\n")
    assert parse_dialog(other, "claude", typed_row=2).fingerprint != d.fingerprint


def test_submit_row_found_below_a_line_left_of_the_text_column(transcript):
    screen = transcript("dialog_ask_multi_typed.txt").replace("         b\n", "b\n")
    d = parse_dialog(screen, "claude", typed_row=2)
    assert d.submit_after == 2
    assert d.options[2].label == "hello a" and d.options[2].free_text
    from herdr_slackbot.dialog import submit_keys
    assert submit_keys(d) == ["down", "enter"]


def test_untyped_type_something_row_keeps_its_label(transcript):
    d = parse(transcript, "dialog_ask_multi.txt")
    assert d.options[3].label == "Type something" and d.options[3].free_text
    assert not any(o.free_text for o in d.options[:3])


# --- review round 3 ---------------------------------------------------------------------------------

def test_row_above_submit_is_a_plain_toggle_without_evidence(transcript):
    """N-A (reviewer's probe): no "Type something" row, so Cherry sits above Submit."""
    screen = transcript("dialog_ask_multi.txt").replace("  4. [ ] Type something\n", "").replace(
        "  5. Chat about this", "  4. Chat about this")
    d = parse_dialog(screen, "claude")
    assert d.submit_after == 2
    cherry = d.options[2]
    assert cherry.label == "Cherry" and cherry.description == "A small, tart fruit full of antioxidants"
    assert not cherry.free_text and d.free_text_option() is None
    assert keys_for(d, 2) == ["3"]  # a toggle


def test_typed_row_needs_the_callers_evidence(transcript):
    from herdr_slackbot.dialog import typed_text
    screen = transcript("dialog_ask_multi_typed.txt")
    plain = parse_dialog(screen, "claude")
    assert not plain.options[2].free_text and plain.options[2].label == "hello a"
    assert parse_dialog(screen, "claude", typed_row=1).options[1].label == "Two"  # not above Submit: ignored
    typed = parse_dialog(screen, "claude", typed_row=2)
    assert typed_text(typed.options[2]) == "hello a\nb"
    assert plain.fingerprint == typed.fingerprint
    untyped = parse(transcript, "dialog_ask_multi.txt")
    assert typed_text(untyped.options[3]) is None  # still labeled "Type something"
