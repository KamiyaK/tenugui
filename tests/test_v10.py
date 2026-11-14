"""0.10.0: UI に 形の集計・内訳と修正案・レポートを開く。ウィンドウなしで Model と画面の組み立てを確かめる。"""
import sys
from unittest.mock import MagicMock

import pytest

from tenugui.ui import Model

TOML = '''schema_version = 1
[[field]]
src = "len"
drop_src = true
[[field.case]]
pattern = "#±#mm"
each = { number_only = true, strip_units = ["mm"] }
parts = { n1 = "nom", n2 = "tol" }
[[field]]
src = "strip"
number_only = true
strip_units = ["mm"]
[[test]]
in = { len = "330±15mm" }
out = { nom = "330", tol = "%s" }
'''


@pytest.fixture
def work(tmp_path):
    (tmp_path / "in.csv").write_text(
        "sheet,len,strip,name\na,330±15mm,7mm,x\nb,330mm,0.7cm,y\nc,340mm,7mm,z\nd,NG,7mm,w\n", encoding="utf-8-sig")
    (tmp_path / "r.toml").write_text(TOML % "15", encoding="utf-8")
    (tmp_path / "bad.toml").write_text(TOML % "99", encoding="utf-8")
    return tmp_path


def test_model_shapes_without_and_with_config(work):
    m = Model()
    rows = m.shapes(str(work / "in.csv"), "")
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("len", 2, 2, "#mm"), ("", "", 1, "#±#mm"), ("strip", 2, 3, "#mm"), ("", "", 1, "#cm")]
    assert rows[0][4] == "330mm / 340mm"
    allrows = m.shapes(str(work / "in.csv"), str(work / "r.toml"), True)
    assert {r[0] for r in allrows} >= {"sheet", "len", "strip", "name"}
    (work / "broken.toml").write_text("schema_version = 1\n[[field]]\nsrc = 3\n", encoding="utf-8")
    assert m.shapes(str(work / "in.csv"), str(work / "broken.toml"))        # 設定が壊れていても形は見られる


def test_model_check_runs_tests(work):
    m = Model()
    assert "テスト: 1/1 件 合格" in m.check(str(work / "in.csv"), str(work / "r.toml"))
    lines = m.check(str(work / "in.csv"), str(work / "bad.toml"))
    assert any("NG テスト" in x for x in lines) and any("期待 '99' / 実際 '15'" in x for x in lines)


def test_model_convert_groups_suggestions_and_report(work):
    m = Model()
    lines = m.convert(str(work / "in.csv"), str(work / "r.toml"), "", False)
    assert m.html_path.endswith("in_tenugui.html") and (work / "tenugui_out" / "in_tenugui.html").exists()
    assert [(g["code"], g["shape"], g["count"], g["suggestion"]["kind"]) for g in m.groups] == [
        ("C111", "#mm", 2, "add_case"), ("C104", "#cm", 1, "use_to")]
    assert any("2 種類" in x for x in lines) and any("書式の内訳 len: #±#mm 1件 / 不一致 2件" in x for x in lines)
    d = m.group_detail(0)
    assert d[0].startswith("2 件  C111  列 len") and "[[field.case]]" in "\n".join(d) and 'pattern = "#mm"' in "\n".join(d)
    assert m.group_detail(9) == []


def test_ui_builds_and_buttons_run_with_fake_tkinter(work, monkeypatch):
    class Var:
        made = []

        def __init__(self, value=""):
            self.v = value
            Var.made.append(self)

        def get(self):
            return self.v

        def set(self, v):
            self.v = v

    buttons = {}

    def button(parent=None, text="", command=None, **kw):
        buttons[text] = command
        return MagicMock()

    tk, ttk = MagicMock(), MagicMock()
    tk.StringVar = tk.BooleanVar = Var
    ttk.Button = button
    tk.ttk = ttk
    mods = {"tkinter": tk, "tkinter.ttk": ttk, "tkinter.filedialog": tk.filedialog,
            "tkinter.messagebox": tk.messagebox, "tkinter.scrolledtext": tk.scrolledtext}
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    import tenugui.ui as ui
    crashes = []
    monkeypatch.setattr(ui.traceback, "format_exc", lambda: crashes.append(sys.exc_info()[1]) or "x")
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u))

    assert ui.main() == 0
    v_in, v_toml, v_out, _verify, _filter, _all, v_col, v_val = Var.made[:8]
    v_in.set(str(work / "in.csv"))
    v_toml.set(str(work / "r.toml"))
    v_out.set(str(work / "tenugui_out" / "in_tenugui.csv"))
    v_col.set("len")
    v_val.set("330±15mm")
    for name in ("形を集計", "集計する", "検証", "変換", "レポートを開く", "試す", "入力CSVから例を入れる", "見る",
                 "出力CSVを読み直して検証"):
        assert name in buttons, name
        buttons[name]()
    assert not crashes, crashes
    assert opened and opened[0].startswith("file://") and opened[0].endswith("in_tenugui.html")
    assert tk.messagebox.showwarning.call_count == 0


def test_mixed_formats_example(capsys):
    from pathlib import Path
    from tenugui.cli import main
    ex = Path(__file__).parents[1] / "examples"
    assert main(["check", "-c", str(ex / "mixed_formats.toml"), str(ex / "mixed_formats_input.csv")]) == 0
    assert "テスト: 4/4 件 合格" in capsys.readouterr().out
