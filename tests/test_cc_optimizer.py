from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_optimizer.analyze import analyze, simulate_compaction
from cc_optimizer.cli import main
from cc_optimizer.report import render
from cc_optimizer.transcripts import TokenCounter, command_group, find_transcripts, parse_timeline

COUNTER = TokenCounter()


def assistant(req: str, blocks: list[dict], ctx: tuple[int, int, int] = (10, 0, 0), out: int = 50) -> dict:
    inp, create, read = ctx
    return {
        "type": "assistant",
        "requestId": req,
        "timestamp": "2026-10-01T10:00:00.000Z",
        "message": {
            "model": "claude-test",
            "content": blocks,
            "usage": {"input_tokens": inp, "cache_creation_input_tokens": create, "cache_read_input_tokens": read,
                      "output_tokens": out},
        },
    }


def tool_use(tid: str, name: str, **inp) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid: str, content, meta: dict | None = None) -> dict:
    entry = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]}}
    if meta:
        entry["toolUseResult"] = meta
    return entry


def write_jsonl(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


BIG = "def handler():\n    return compute(value)\n" * 400


def sample_session() -> list[dict]:
    return [
        {"type": "ai-title", "aiTitle": "Örnek oturum"},
        {"type": "user", "message": {"content": "Projeyi incele ve testleri çalıştır."}},
        # One response split over two lines with the same requestId: must count once.
        assistant("r1", [{"type": "thinking", "thinking": "plan"}], ctx=(5, 20_000, 0), out=10),
        assistant("r1", [tool_use("t1", "Read", file_path="C:\\src\\app.py")], ctx=(5, 20_000, 0), out=80),
        tool_result("t1", BIG, {"type": "text"}),
        assistant("r2", [tool_use("t2", "Bash", command='cd "C:/repo x" 2>/dev/null && npm test -- --watch=false')],
                  ctx=(5, 9_000, 20_000)),
        tool_result("t2", "FAIL " * 2000),
        assistant("r3", [tool_use("t3", "Read", file_path="c:/src/app.py")], ctx=(5, 4_000, 29_000)),
        tool_result("t3", BIG, {"type": "text"}),  # redundant: same file, no edit in between
        {"type": "attachment", "attachment": {"type": "prompt_snapshot", "systemPrompt": ["x" * 5000]}},
        {"type": "attachment", "attachment": {"type": "queued_command", "prompt": [
            {"type": "image", "source": {"type": "base64", "data": "A" * 10_000}}]}},
        assistant("r4", [tool_use("t4", "Edit", file_path="C:/src/app.py", old_string="a", new_string="b")],
                  ctx=(5, 9_000, 33_000)),
        tool_result("t4", "ok"),
        assistant("r5", [tool_use("t5", "Read", file_path="C:/src/app.py")], ctx=(5, 500, 42_000)),
        tool_result("t5", BIG, {"type": "text"}),  # not redundant: file edited
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger": "auto", "preTokens": 51_000, "postTokens": 8_000}},
        {"type": "user", "isCompactSummary": True, "message": {"content": "Özet: testler düzeltildi."}},
        assistant("r6", [{"type": "text", "text": "Bitti."}], ctx=(5, 8_000, 0), out=5),
        {"type": "assistant", "requestId": "s1", "message": {"model": "<synthetic>", "content": [], "usage": {}}},
    ]


def test_parse_timeline(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", sample_session())
    tl = parse_timeline(path, "proj", "s", False, COUNTER)

    assert tl.title == "Örnek oturum"
    assert len(tl.calls) == 6  # r1 deduplicated, synthetic skipped
    assert tl.calls[0].output == 80 and tl.calls[0].cache_creation == 20_000
    assert len(tl.compactions) == 1 and tl.compactions[0].position == 5

    tools = [it for it in tl.items if it.kind == "tool"]
    assert [t.name for t in tools] == ["Read", "Bash", "Read", "Edit", "Read"]
    first_read = tools[0]
    assert (first_read.start, first_read.end, first_read.carried_calls) == (1, 5, 4)
    assert tools[1].group == "npm test"
    assert tl.edits == [(4, "c:/src/app.py")]

    kinds = {(it.kind, it.name) for it in tl.items}
    assert ("attachment", "prompt_snapshot") not in kinds
    queued = next(it for it in tl.items if it.name == "queued_command")
    assert queued.images == 1 and queued.tokens < 2000  # base64 counted as one image, not as text
    summary = next(it for it in tl.items if it.kind == "summary")
    assert summary.start == 5 and summary.end == 6


def test_analyze_and_render(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", sample_session())
    tl = parse_timeline(path, "proj", "s", False, COUNTER)
    rep = analyze([tl], top=5, estimate_method=COUNTER.method)

    assert rep["tokens"]["calls"] == 6
    assert rep["reads"]["redundant"]["count"] == 1
    assert rep["context"]["compactions"] == {"auto": 1}
    names = {s["name"] for s in rep["sources"]}
    assert {"tool:Read", "tool:Bash", "fixed", "model_output", "prompt", "summary"} <= names
    assert rep["top_outputs"][0]["tool"] == "Read"
    assert len(rep["context"]["what_if_compaction"]) == 4

    text = render(rep)
    assert "BAĞLAMI NE DOLDURUYOR" in text
    assert "Örnek oturum" in text


def test_simulated_compaction_saves_tokens_on_long_sessions(tmp_path):
    entries = []
    for i in range(50):
        entries.append(assistant(f"r{i}", [{"type": "text", "text": "x"}], ctx=(0, 1000, 10_000 + i * 10_000)))
    tl = parse_timeline(write_jsonl(tmp_path / "long.jsonl", entries), "p", "long", False, COUNTER)

    low = simulate_compaction([tl], threshold=100_000, post_tokens=10_000)
    high = simulate_compaction([tl], threshold=10_000_000, post_tokens=10_000)
    assert high["saving_share"] == 0 and high["extra_compactions"] == 0
    assert 0 < low["saving_share"] < 1 and low["extra_compactions"] > 0


@pytest.mark.parametrize(
    ("command", "group"),
    [
        ('cd "C:/a b" 2>/dev/null && pnpm --filter api test', "pnpm api"),
        ('S="x"\nls -la "$S"', "ls"),
        (".venv/Scripts/python -m pytest -q", "python pytest"),
        ("python - <<'PY'\nprint(1)\nPY", "python"),
        ("git -C repo status", "git repo"),
        ("Set-Location C:/x; Get-ChildItem", "get-childitem"),
        ("# note\ngrep -r foo .", "grep"),
        ("FOO=1 npm run build", "npm run"),
        ("cd somewhere", "cd"),
    ],
)
def test_command_group(command, group):
    assert command_group(command) == group


def test_find_transcripts_and_cli(tmp_path, capsys):
    root = tmp_path / "projects"
    project = root / "c--work-demo"
    write_jsonl(project / "abc123.jsonl", sample_session())
    write_jsonl(project / "abc123" / "subagents" / "agent-1.jsonl", sample_session()[2:6])
    write_jsonl(root / "other" / "zzz.jsonl", sample_session())

    found = list(find_transcripts(root, session="abc"))
    assert [(f[1], f[2], f[3]) for f in found] == [("c--work-demo", "abc123", False), ("c--work-demo", "abc123", True)]

    assert main(["analyze", "--root", str(root), "--session", "abc", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["scope"]["sessions"] == 1 and rep["scope"]["subagent_timelines"] == 1
