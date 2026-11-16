"""0.12.0: 表の書き出し（CSV / xlsx）。survey -o と UI の「表示中のタブを書き出す」が使う export_tables。"""
import csv
import json
import sys
import zipfile
import xml.etree.ElementTree as ET

from tenugui.cli import main
from tenugui.export import export_tables
from tenugui.ui import Model

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_xlsx(p):
    with zipfile.ZipFile(p) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        names = [s.get("name") for s in wb.find("m:sheets", NS)]
        out = {}
        for i, n in enumerate(names):
            sh = ET.fromstring(z.read(f"xl/worksheets/sheet{i + 1}.xml"))
            rows = []
            for r in sh.iter(f"{{{NS['m']}}}row"):
                vals = []
                for c in r:
                    col = ord(c.get("r")[0]) - 65          # テストの表は26列未満
                    vals += [""] * (col - len(vals))
                    t = c.find("m:is/m:t", NS)
                    v = c.find("m:v", NS)
                    vals.append(t.text if t is not None else (v.text if v is not None else ""))
                rows.append(vals)
            out[n] = rows
        return out


def test_csv_single(tmp_path):
    p = tmp_path / "a.csv"
    assert export_tables(p, [("形", ["列", "件数"], [["x", 2], ["", 1]])]) == [p]
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")
    assert list(csv.reader(p.open(encoding="utf-8-sig"))) == [["列", "件数"], ["x", "2"], ["", "1"]]


def test_csv_multi_splits_files(tmp_path):
    files = export_tables(tmp_path / "v.csv", [("前後 列", ["a"], [["1"]]), ("違反", ["b"], [])])
    assert [f.name for f in files] == ["v_前後_列.csv", "v_違反.csv"]


def test_xlsx_sheets_and_text_kept(tmp_path):
    p = tmp_path / "a.xlsx"
    export_tables(p, [("形", ["列", "件数", "例"], [["x", 2, "001 / <35mm> & \"a\""]]), ("形", ["b"], [])])
    got = read_xlsx(p)
    assert list(got) == ["形", "形_2"]
    assert got["形"] == [["列", "件数", "例"], ["x", "2", "001 / <35mm> & \"a\""]]


def test_model_shapes_unchanged_and_survey_output(tmp_path, capsys):
    (tmp_path / "in.csv").write_text("sheet,len\na,330±15mm\nb,330mm\nc,340mm\n", encoding="utf-8-sig")
    rows = Model().shapes(str(tmp_path / "in.csv"), "")
    assert rows == [("len", 2, 2, "#mm", "330mm / 340mm"), ("", "", 1, "#±#mm", "330±15mm")]
    out = tmp_path / "o" / "s.xlsx"
    capsys.readouterr()
    assert main(["survey", str(tmp_path / "in.csv"), "-o", str(out), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["files"] == [str(out)]
    assert read_xlsx(out)["形"] == [["列", "形の種類", "件数", "形", "例"], ["len", "2", "2", "#mm", "330mm / 340mm"],
                                    ["", "", "1", "#±#mm", "330±15mm"]]


def test_survey_output_with_column_is_usage_error(tmp_path):
    (tmp_path / "in.csv").write_text("sheet,len\na,1mm\n", encoding="utf-8-sig")
    assert main(["survey", str(tmp_path / "in.csv"), "-k", "len", "-o", str(tmp_path / "x.csv")]) == 2
    assert not (tmp_path / "x.csv").exists()


def test_ui_export_current_tab_with_fake_tkinter(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    (tmp_path / "in.csv").write_text("sheet,len\na,330±15mm\nb,330mm\nc,340mm\n", encoding="utf-8-sig")

    class Var:
        def __init__(self, value=""):
            self.v = value

        def get(self):
            return self.v

        def set(self, v):
            self.v = v

    class Tree(MagicMock):
        def __init__(self, *a, columns=(), **kw):
            super().__init__()
            self.__dict__["cols"], self.__dict__["data"] = list(columns), []

        def cget(self, k):
            return tuple(self.cols)

        def insert(self, parent, index, iid=None, values=()):
            self.data.append(list(values))

        def delete(self, *items):
            self.data.clear()

        def get_children(self):
            return list(range(len(self.data)))

        def item(self, i, opt):
            return tuple(self.data[i])

    class Notebook(MagicMock):
        def __init__(self, *a, **kw):
            super().__init__()
            self.__dict__["tabs"], self.__dict__["cur"] = {}, None

        def add(self, w, text=""):
            self.tabs[id(w)] = (w, text)

        def select(self, w=None):
            if w is None:
                return self.cur
            self.__dict__["cur"] = id(w)

        def nametowidget(self, k):
            return self.tabs[k][0]

        def tab(self, w, opt):
            return self.tabs[id(w)][1]

    buttons, vars_ = {}, []

    def button(parent=None, text="", command=None, **kw):
        buttons[text] = command
        return MagicMock()

    def var(value=""):
        vars_.append(Var(value))
        return vars_[-1]

    tk, ttk = MagicMock(), MagicMock()
    tk.StringVar = tk.BooleanVar = var
    ttk.Button, ttk.Treeview, ttk.Notebook = button, Tree, Notebook
    ttk.Frame = lambda *a, **kw: MagicMock()
    tk.ttk = ttk
    out = tmp_path / "exp.xlsx"
    tk.filedialog.asksaveasfilename = lambda **kw: str(out)
    for k, v in {"tkinter": tk, "tkinter.ttk": ttk, "tkinter.filedialog": tk.filedialog,
                 "tkinter.messagebox": tk.messagebox, "tkinter.scrolledtext": tk.scrolledtext}.items():
        monkeypatch.setitem(sys.modules, k, v)
    import tenugui.ui as ui
    crashes = []
    monkeypatch.setattr(ui.traceback, "format_exc", lambda: crashes.append(sys.exc_info()[1]) or "x")
    ui.main()
    vars_[0].set(str(tmp_path / "in.csv"))        # 入力CSV
    buttons["形を集計"]()                          # 形タブを選んで表示する
    assert not crashes
    buttons["表示中のタブを書き出す"]()
    assert read_xlsx(out)["形"] == [["列", "形の種類", "件数", "形", "例"], ["len", "2", "2", "#mm", "330mm / 340mm"],
                                    ["", "", "1", "#±#mm", "330±15mm"]]
