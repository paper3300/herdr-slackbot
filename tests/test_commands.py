from herdr_slackbot.commands import (
    NewArgs,
    parse_command,
    parse_new_args,
    parse_send_args,
    resolve_agent,
    resolve_workspace,
    workspace_cwd,
)


def test_parse_command():
    assert parse_command("") == ("", "")
    assert parse_command("  LIST ") == ("list", "")
    assert parse_command("send coder fix\nthe bug") == ("send", "coder fix\nthe bug")


def test_parse_new_args_full():
    got = parse_new_args('"My WS" name=fix-1 kind=codex model=gpt-6-sol effort=max cwd=D:\\x do the thing\nline2')
    assert got == NewArgs("My WS", "do the thing\nline2", "fix-1", "codex", "gpt-6-sol", "max", None, "D:\\x")


def test_parse_new_args_minimal_and_errors():
    assert parse_new_args("DemoApp hello world") == NewArgs("DemoApp", "hello world")
    assert "workspace" in parse_new_args("")
    assert "prompt" in parse_new_args("DemoApp name=x")
    assert "kind" in parse_new_args("DemoApp kind=gemini hi")
    # key=value that is not an option starts the prompt
    assert parse_new_args("ws a=b c").prompt == "a=b c"


def test_parse_send_args():
    assert parse_send_args("coder please fix\nit") == ("coder", "please fix\nit")
    assert parse_send_args("w3:p28 hi") == ("w3:p28", "hi")
    assert "target" in parse_send_args("")
    assert "prompt" in parse_send_args("coder")


def test_resolve_workspace():
    wss = [{"workspace_id": "w1", "label": "Main"}, {"workspace_id": "w2", "label": "main"},
           {"workspace_id": "w3", "label": "DemoApp"}]
    assert resolve_workspace(wss, "w1")["workspace_id"] == "w1"
    assert resolve_workspace(wss, "demoapp")["workspace_id"] == "w3"
    assert resolve_workspace(wss, "main") is None  # ambiguous
    assert resolve_workspace(wss, "nope") is None


def test_resolve_agent():
    agents = [{"pane_id": "w1:p1", "name": "coder"}, {"pane_id": "coder"}, {"pane_id": "w1:p2", "name": None}]
    assert resolve_agent(agents, "coder")["pane_id"] == "w1:p1"  # names win over pane ids
    assert resolve_agent(agents, "w1:p2")["pane_id"] == "w1:p2"
    assert resolve_agent(agents, "x") is None


def test_workspace_cwd():
    panes = [{"pane_id": "a", "cwd": None}, {"pane_id": "b", "cwd": "D:\\shell\\"},
             {"pane_id": "c", "cwd": "D:\\agent\\", "agent": "claude"}]
    assert workspace_cwd(panes) == "D:\\agent"
    assert workspace_cwd([{"cwd": "C:\\"}]) == "C:\\"
    assert workspace_cwd([]) == ""


# --- review #11: quoted values inside key=value options ----------------------------------------------

def test_review11_quoted_windows_cwd_with_spaces():
    got = parse_new_args(r'Main cwd="D:\My Project" inspect files')
    assert got.cwd == r"D:\My Project" and got.prompt == "inspect files"


def test_review11_quoted_workspace_and_multiline_prompt_verbatim():
    got = parse_new_args('"My WS" name=fix-1 cwd="C:\\Program Files\\x" do "this"\nand that')
    assert (got.workspace, got.name, got.cwd) == ("My WS", "fix-1", r"C:\Program Files\x")
    assert got.prompt == 'do "this"\nand that'


def test_review11_unmatched_quote_is_rejected():
    assert "Unmatched" in parse_new_args(r'Main cwd="D:\My Project inspect')
    assert "Unmatched" in parse_new_args('"My WS hi')
    assert "Unmatched" in parse_send_args('"coder hi')


def test_review11_quoted_send_target():
    assert parse_send_args('"my agent" hi "there"') == ("my agent", 'hi "there"')
