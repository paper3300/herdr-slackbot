import json

import pytest

from herdr_slackbot import blocks as B


def all_options(view):
    for block in view["blocks"]:
        el = block.get("element") or {}
        yield from el.get("options") or []


def block_ids(view):
    return [b.get("block_id") for b in view["blocks"]]


WORKSPACES = [{"workspace_id": "w2", "label": "Sandbox"}, {"workspace_id": "w3", "label": "DemoApp"}]
CODEX_MODELS = [("gpt-6-sol", "GPT-6-Sol"), ("gpt-6-astra", "GPT-6-Astra")]


def test_escape_and_mrkdwn_conversion():
    md = "# Title\n**bold** and __b2__ <tag> & [link](https://x.y/a?b=1)\n- item\n```\n**not bold** <x>\n```"
    out = B.to_mrkdwn(md)
    lines = out.split("\n")
    assert lines[0] == "*Title*"
    assert "*bold* and *b2* &lt;tag&gt; &amp; <https://x.y/a?b=1|link>" == lines[1]
    assert lines[2] == "• item"
    assert "**not bold** &lt;x&gt;" in out  # code fence untouched except escaping


def test_to_mrkdwn_closes_open_fence():
    assert B.to_mrkdwn("```\ncode").endswith("```")


def test_usage_mentions_all_commands():
    text = B.usage_text("/herdr-me")
    for sub in ("list", "new", "send", "status"):
        assert f"/herdr-me {sub}" in text


def test_option_text_and_value_limits():
    opt = B.option("x" * 200, "v" * 400)
    assert len(opt["text"]["text"]) <= B.OPTION_TEXT_MAX
    assert len(opt["value"]) <= B.OPTION_VALUE_MAX


def test_new_agent_view_claude_fields():
    st = B.NewModalState(workspace_id="w3", cwd=r"D:\proj", metadata={"channel": "C1"})
    view = B.new_agent_view(st, WORKSPACES, CODEX_MODELS, "gpt-6-sol")
    assert view["callback_id"] == B.NEW_CALLBACK and len(view["title"]["text"]) <= 24
    ids = block_ids(view)
    assert ids == ["ws", "cwd:w3", "kind", "model:claude", "effort:claude", "perm", "name", "prompt"]
    by_id = {b["block_id"]: b for b in view["blocks"]}
    assert by_id["ws"]["dispatch_action"] and by_id["kind"]["dispatch_action"]
    assert by_id["ws"]["element"]["initial_option"]["value"] == "w3"
    assert by_id["cwd:w3"]["element"]["initial_value"] == r"D:\proj"
    assert by_id["model:claude"]["element"]["initial_option"]["value"] == "opus"
    assert by_id["effort:claude"]["element"]["initial_option"]["value"] == "high"
    perms = [o["value"] for o in by_id["perm"]["element"]["options"]]
    assert perms == ["manual", "acceptEdits", "auto", "plan"]
    assert by_id["perm"]["element"]["initial_option"]["value"] == "auto"
    assert by_id["prompt"]["element"]["multiline"] is True
    assert by_id["name"]["optional"] is True
    assert json.loads(view["private_metadata"]) == {"channel": "C1"}
    assert all(len(o["text"]["text"]) <= 75 for o in all_options(view))


def test_new_agent_view_codex_swaps_fields():
    st = B.NewModalState(workspace_id="w2", kind="codex")
    view = B.new_agent_view(st, WORKSPACES, CODEX_MODELS, "gpt-6-sol")
    ids = block_ids(view)
    assert "perm" not in ids
    assert "model:codex" in ids and "effort:codex" in ids
    by_id = {b["block_id"]: b for b in view["blocks"]}
    assert [o["value"] for o in by_id["model:codex"]["element"]["options"]] == ["gpt-6-sol", "gpt-6-astra"]
    assert by_id["model:codex"]["element"]["initial_option"]["value"] == "gpt-6-sol"
    assert [o["value"] for o in by_id["effort:codex"]["element"]["options"]] == \
        ["low", "medium", "high", "xhigh", "max"]
    assert "initial_value" not in by_id["cwd:w2"]["element"]  # empty cwd -> no initial_value


def test_selects_cap_at_100_options():
    many = [(f"m{i}", f"Model {i}") for i in range(150)]
    view = B.new_agent_view(B.NewModalState(workspace_id="w2", kind="codex"), WORKSPACES, many, "m0")
    model_block = next(b for b in view["blocks"] if b["block_id"] == "model:codex")
    assert len(model_block["element"]["options"]) == 100


def _values(view_blocks_state):
    return view_blocks_state


def test_parse_new_view_state_roundtrip():
    values = {
        "ws": {"new_ws": {"type": "static_select", "selected_option": {"value": "w3"}}},
        "cwd:w3": {"value": {"type": "plain_text_input", "value": "  D:\\p  "}},
        "kind": {"new_kind": {"type": "static_select", "selected_option": {"value": "claude"}}},
        "model:claude": {"value": {"type": "static_select", "selected_option": {"value": "sonnet"}}},
        "effort:claude": {"value": {"type": "static_select", "selected_option": {"value": "max"}}},
        "perm": {"value": {"type": "static_select", "selected_option": {"value": "plan"}}},
        "name": {"value": {"type": "plain_text_input", "value": None}},
        "prompt": {"value": {"type": "plain_text_input", "value": "do it\nnow"}},
    }
    st = B.parse_new_view_state(values)
    assert (st.workspace_id, st.cwd, st.kind, st.model, st.effort, st.permission_mode, st.name, st.prompt) == \
        ("w3", "D:\\p", "claude", "sonnet", "max", "plan", "", "do it\nnow")
    assert B.parse_new_view_state({}).kind == "claude"


def test_send_view_and_parse():
    agents = [{"pane_id": "w3:p28", "agent_status": "done", "workspace_id": "w3", "name": None,
               "terminal_title_stripped": "t" * 100},
              {"pane_id": "wD:p4", "agent_status": "working", "workspace_id": "wD", "name": "coder"}]
    view = B.send_view(agents, {"w3": "DemoApp"}, initial_target="coder")
    target = view["blocks"][0]["element"]
    assert [o["value"] for o in target["options"]] == ["w3:p28", "coder"]
    assert target["initial_option"]["value"] == "coder"
    assert all(len(o["text"]["text"]) <= 75 for o in target["options"])
    values = {"target": {"value": {"selected_option": {"value": "coder"}}},
              "prompt": {"value": {"value": "hello"}}}
    assert B.parse_send_view_state(values) == ("coder", "hello")


def test_agent_list_blocks_chunks_sections():
    agents = [{"pane_id": f"w1:p{i}", "workspace_id": "w1", "agent_status": "idle", "agent": "claude",
               "terminal_title_stripped": "x" * 60} for i in range(200)]
    blocks = B.agent_list_blocks(agents, {"w1": "Main"})
    assert len(blocks) <= B.MAX_BLOCKS
    assert all(len(b["text"]["text"]) <= B.SECTION_MAX for b in blocks if b["type"] == "section")
    assert "*Herdr agents* (200)" in blocks[0]["text"]["text"]
    assert B.agent_list_blocks([], {})[0]["text"]["text"].startswith("No agents")


def test_thread_root_and_mute_toggle():
    blocks = B.thread_root_blocks("🤖 *coder* · ws", ["`claude`"], "sess-1", muted=False)
    button = blocks[-1]["elements"][0]
    assert blocks[-1]["block_id"] == B.BLOCK_THREAD_CTL
    assert button["action_id"] == B.ACTION_MUTE and "Mute" in button["text"]["text"]
    assert json.loads(button["value"]) == {"s": "sess-1", "m": True}
    toggled = B.with_mute_button(blocks, "sess-1", muted=True)
    assert len(toggled) == 2 and toggled[0] == blocks[0]
    assert "Unmute" in toggled[-1]["elements"][0]["text"]["text"]
    assert json.loads(toggled[-1]["elements"][0]["value"]) == {"s": "sess-1", "m": False}


def test_result_blocks_short():
    blocks, truncated = B.result_blocks("✅ *a* · ws · 3s", ["📁 `D:\\x`"], "**done**", None, recap="summary")
    assert not truncated
    assert [b["type"] for b in blocks] == ["section", "context", "context", "section"]
    assert blocks[-1]["text"]["text"] == "*done*"


def test_result_blocks_truncated_has_full_button_and_limits():
    body = "\n".join(f"line {i} " + "y" * 80 for i in range(100))
    blocks, truncated = B.result_blocks("h", [], body, "abc123", limit=3000)
    assert truncated
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")
    actions = blocks[-1]
    assert actions["elements"][0]["action_id"] == B.ACTION_SHOW_FULL
    assert actions["elements"][0]["value"] == "abc123"
    assert actions["elements"][0]["text"]["text"] == "전체 보기"
    # No id -> no button (used to probe truncation first)
    blocks2, _ = B.result_blocks("h", [], body, None)
    assert blocks2[-1]["type"] == "section"


def test_result_blocks_raw_output_keeps_fence_closed():
    body = "x" * 5000
    blocks, truncated = B.result_blocks("h", [], body, "id", markdown=False, limit=3000)
    text = blocks[-2]["text"]["text"]
    assert truncated and text.startswith("```") and text.endswith("```") and len(text) <= 3000


def test_result_blocks_escape():
    blocks, _ = B.result_blocks("h", [], "<@U123> & <!channel>", None)
    assert blocks[-1]["text"]["text"] == "&lt;@U123&gt; &amp; &lt;!channel&gt;"


def test_misc_blocks():
    assert "started" in B.started_blocks()[0]["elements"][0]["text"]
    assert "is waiting for your answer" in B.dialog_header("a<b", "ws")
    assert "a&lt;b" in B.dialog_header("a<b", "ws")
    assert B.sent_blocks("  hi\n there ")[0]["text"]["text"] == "📨 hi there"


@pytest.mark.parametrize("n", [0, 1, 5000])
def test_chunk_lines(n):
    chunks = B.chunk_lines(["z" * 100] * n, 3000)
    assert all(len(c) <= 3000 for c in chunks)
    assert sum(c.count("z" * 100) for c in chunks) == n


# --- blocked dialogs ---------------------------------------------------------------------------

def _dialog(transcript, name, kind="claude"):
    from herdr_slackbot.dialog import parse_dialog
    return parse_dialog(transcript(name), kind)


def _buttons(blocks):
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


def _mrkdwn(blocks):
    """All mrkdwn text of sections and contexts."""
    out = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    out += [e["text"] for b in blocks if b["type"] == "context" for e in b["elements"]]
    return "\n".join(out)


def test_dialog_blocks_permission(transcript):
    d = _dialog(transcript, "dialog_claude_permission.txt")
    blocks = B.dialog_blocks("coder", "Main", d, "tok")
    text = _mrkdwn(blocks)
    assert blocks[0]["text"]["text"] == "⚠️ *coder* · Main is waiting for your answer"
    assert "```\necho spike &gt; a.txt" in text  # the command in a code block
    assert "*Do you want to proceed?*" in text
    assert "from this project" in text  # the full label stays in the section text
    buttons = _buttons(blocks)
    assert [b["action_id"] for b in buttons] == ["dlg:opt:0", "dlg:opt:1", "dlg:opt:2", "dlg:key:esc", "dlg:screen"]
    assert buttons[0]["text"]["text"] == "1. Yes"
    assert len(buttons[1]["text"]["text"]) <= B.OPTION_TEXT_MAX and buttons[1]["text"]["text"].endswith("…")
    assert json.loads(buttons[1]["value"]) == {"t": "tok", "o": 1}
    assert json.loads(buttons[3]["value"]) == {"t": "tok", "o": "esc"}
    assert all(len(b["value"]) < 100 for b in buttons)  # no screen text in values


def test_dialog_blocks_question_free_text_and_chat(transcript):
    d = _dialog(transcript, "dialog_ask_single.txt")
    blocks = B.dialog_blocks("coder", "Main", d, "tok")
    ids = [b["action_id"] for b in _buttons(blocks)]
    assert ids == ["dlg:opt:0", "dlg:opt:1", "dlg:opt:2", "dlg:text:3", "dlg:opt:4", "dlg:key:esc", "dlg:screen"]
    text = json.dumps(blocks, ensure_ascii=False)
    assert "☐ Color · ☐ Fruits" in text and "*Pick a color?*" in text
    assert "A warm, vibrant color" in text


def test_dialog_blocks_multi_select_has_checkboxes_and_next(transcript):
    d = _dialog(transcript, "dialog_ask_multi.txt")
    buttons = _buttons(B.dialog_blocks("coder", "Main", d, "tok"))
    assert buttons[0]["text"]["text"] == "☐ 1. Apple"
    assert "dlg:key:submit" in [b["action_id"] for b in buttons]  # Next -> = Submit row + Enter
    assert "dlg:key:right" not in [b["action_id"] for b in buttons]
    toggled = _dialog(lambda n: transcript(n).replace("1. [ ] Apple", "1. [✔] Apple"), "dialog_ask_multi.txt")
    assert _buttons(B.dialog_blocks("coder", "Main", toggled, "tok"))[0]["text"]["text"] == "☑ 1. Apple"


def test_dialog_blocks_plan_with_full_text_button(transcript):
    d = _dialog(transcript, "dialog_plan.txt")
    short = B.dialog_blocks("coder", "Main", d, "tok")
    assert "Create b.txt" in json.dumps(short) and "show_full" not in json.dumps(short)
    long_plan = "# Plan\n" + "\n".join(f"- step {i} with some words" for i in range(300))
    blocks = B.dialog_blocks("coder", "Main", d, "tok", plan_text=long_plan, full_id="rid")
    full = [b for b in _buttons(blocks) if b["action_id"] == B.ACTION_SHOW_FULL]
    assert full and full[0]["value"] == "rid" and full[0]["text"]["text"] == "전체 보기"
    assert all(len(b["text"]["text"]) <= B.SECTION_MAX for b in blocks if b["type"] == "section")
    assert "*Plan*" in json.dumps(blocks)  # markdown converted
    assert "dlg:text:2" in [b["action_id"] for b in _buttons(blocks)]


def test_dialog_blocks_keypad_when_not_parsed():
    tail = [f"line {i}" for i in range(30)]
    blocks = B.dialog_blocks("coder", "Main", None, "tok", screen_tail=tail)
    text = json.dumps(blocks)
    assert "line 29" in text and "line 14" not in text  # last 15 lines
    ids = [b["action_id"] for b in _buttons(blocks)]
    assert ids == ["dlg:key:1", "dlg:key:2", "dlg:key:3", "dlg:key:4", "dlg:key:up", "dlg:key:down",
                   "dlg:key:enter", "dlg:key:esc", "dlg:screen"]


def test_dialog_blocks_escape_and_limits():
    from herdr_slackbot.dialog import Dialog, Option
    options = tuple(Option(i, f"<b>{'x' * 200}", "desc " * 100) for i in range(1, 31))
    d = Dialog("permission", "T<", ("```evil```", "y" * 5000), "Q?", options)
    blocks = B.dialog_blocks("n<", "w", d, "tok")
    assert len(blocks) <= B.MAX_BLOCKS
    assert all(len(b["text"]["text"]) <= B.SECTION_MAX for b in blocks if b["type"] == "section")
    assert all(len(b["elements"]) <= 25 for b in blocks if b["type"] == "actions")
    text = _mrkdwn(blocks)
    assert "<b>" not in text and "&lt;b&gt;" in text and "n&lt;" in text
    assert "```evil" not in text  # a fence inside the body is defused
    block_ids = [b["block_id"] for b in blocks if b.get("block_id")]
    assert len(block_ids) == len(set(block_ids))


def test_dialog_value_and_text_view_round_trip():
    assert B.parse_dialog_value('{"t": "tok", "o": 2}') == ("tok", 2)
    assert B.parse_dialog_value("junk") == (None, None)
    view = B.dialog_text_view("tok", 3, "coder", "Type something.", "Pick a name?",
                              {"c": "D1", "m": "1.2", "th": "1.0"})
    assert view["callback_id"] == B.DIALOG_TEXT_CALLBACK
    view["state"] = {"values": {B.BLOCK_DIALOG_TEXT: {B.ACTION_VALUE: {"value": "베타\n둘째 줄"}}}}
    assert B.parse_dialog_text_view(view) == ("tok", 3, "베타\n둘째 줄", {"c": "D1", "m": "1.2", "th": "1.0"})
    bare = B.dialog_text_view("tok", 0, "coder", "Type something.")
    assert B.parse_dialog_text_view(bare)[3] == {}


def test_dialog_closed_and_without_actions():
    blocks = B.dialog_closed_blocks("coder", "Main", "Bash command — Do you want to proceed?",
                                    "✅ *1. Yes* — answered from Slack")
    assert not any(b["type"] == "actions" for b in blocks)
    assert blocks[-1]["text"]["text"] == "✅ *1. Yes* — answered from Slack"
    stripped = B.without_actions([{"type": "section", "text": B.mrkdwn("x")}, {"type": "actions", "elements": []}],
                                 "This question is no longer open.")
    assert [b["type"] for b in stripped] == ["section", "context"]


def test_dialog_blocks_split_many_options_and_clip_huge_labels():
    from herdr_slackbot.dialog import Dialog, Option
    options = tuple(Option(i, "L" * 3500 if i == 1 else f"option {i}") for i in range(1, 28))
    blocks = B.dialog_blocks("coder", "Main", Dialog("unknown", "T", (), "Q?", options), "tok")
    option_blocks = [b for b in blocks if b["type"] == "actions" and b["block_id"].startswith(B.BLOCK_DIALOG_OPTIONS)]
    assert [len(b["elements"]) for b in option_blocks] == [25, 2]
    assert [b["block_id"] for b in option_blocks] == ["dialog_options", "dialog_options1"]
    assert all(len(b["text"]["text"]) <= B.SECTION_MAX for b in blocks if b["type"] == "section")
    assert len(_buttons(blocks)[0]["text"]["text"]) <= B.OPTION_TEXT_MAX
