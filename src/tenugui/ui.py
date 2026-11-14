"""ざっくりUI（tkinter・標準ライブラリのみ）。処理は core/analysis を呼ぶだけの薄い層。

起動: tenugui ui   または   python -m tenugui.ui
"""
from __future__ import annotations

import traceback
from pathlib import Path

from . import __version__
from .analysis import (convert_to_files, draft_toml, header_drift, lint, run_tests, suggest, survey_overview,
                       try_row, verify_files)
from .cli import default_out_dir, resolve_output
from .core import ConfigError, check_header, config_from_dict, load_config, read_csv, uniquify

PREVIEW_ROWS = 200


class Model:
    """UIから独立した状態と処理。ウィンドウ無しでテストできる。"""

    def __init__(self) -> None:
        self.cfg = None
        self.header: list[str] = []
        self.rows: list[list[str]] = []
        self.out_header: list[str] = []
        self.out_rows: list[list[str]] = []
        self.issues: list[dict] = []
        self.changes: list[dict] = []
        self.verification: dict = {}
        self.out_path = self.out_dir = self.html_path = ""
        self.groups: list[dict] = []        # 問題の内訳 + 修正案
        self.coverage: list[dict] = []      # 書式の内訳（case 形式の列）

    def load(self, csv_path: str, toml_path: str) -> None:
        self.cfg = load_config(toml_path) if toml_path else config_from_dict({"schema_version": 1})
        self.header, self.rows = read_csv(csv_path, self.cfg.encoding_in)

    def check(self, csv_path: str, toml_path: str) -> list[str]:
        self.load(csv_path, toml_path)
        uh, dups = uniquify(self.header)
        lines = [f"設定OK: field {len(self.cfg.fields)} 件 / derive {len(self.cfg.derives)} 件"]
        lines += [f"NG {p}" for p in check_header(self.cfg, uh)]
        t = run_tests(self.cfg)
        if t["total"]:
            lines.append(f"テスト: {t['total'] - t['failed']}/{t['total']} 件 合格")
        for f in t["failures"]:
            lines.append(f"NG テスト {f['name']}: 入力 {f['in']}")
            lines += [f"     {d['column']}: 期待 {d['expected']!r} / 実際 {d['actual']!r}" for d in f["diffs"]]
        lines += [f"注意 {w['code']} {w['where']}: {w['message']}" for w in lint(self.cfg)]
        lines += [f"注意: 列名 {d!r} が重複（#2.. を付けて区別）" for d in dups]
        dr = header_drift(toml_path, self.header) if toml_path else None
        if dr and dr["changed"]:
            lines.append(f"注意 C003 前回(lock)から列が変わっています 増:{dr['added']} 減:{dr['removed']}")
        return lines

    def default_output(self, csv_path: str) -> str:
        return str(default_out_dir(csv_path) / f"{Path(csv_path).stem}_tenugui.csv")

    def convert(self, csv_path: str, toml_path: str, out_path: str, verify: bool = False) -> list[str]:
        self.load(csv_path, toml_path)
        uh, _ = uniquify(self.header)
        probs = check_header(self.cfg, uh)
        if probs:
            raise ConfigError(probs)
        out = resolve_output(csv_path, out_path or None, None, False)   # tenugui_out/ が無ければ作る
        r = convert_to_files(self.cfg, toml_path, csv_path, self.header, self.rows, out, verify=verify,
                             command="tenugui ui")
        self.out_header, self.out_rows = r["out_header"], r["out_rows"]
        self.issues, self.changes, self.verification = r["idicts"], r["changes"], r["verification"]
        self.out_path, self.out_dir, self.html_path = str(out), str(out.parent), r["files"]["html"]
        self.coverage = r["coverage"]
        self.groups = suggest(self.cfg, self.header, self.rows) if r["issues"] else []
        other = [g for g in r["groups"] if not any(
            (g["column"], g["out"], g["code"], g["shape"]) == (x["column"], x["out"], x["code"], x["shape"])
            for x in self.groups)]
        self.groups += [{**g, "suggestion": {"kind": "none", "toml": "", "confidence": "none",
                                             "note": g["hint"] or "自動の案はありません"}} for g in other]
        self.groups.sort(key=lambda g: -g["count"])
        s = self.verification["summary"]
        lines = [f"{len(self.out_rows)} 行を変換 → {out}",
                 f"エラー {r['errors']} 件 / 警告 {r['warnings']} 件"
                 + (f"（{len(self.groups)} 種類 →「内訳と修正案」タブ）" if self.groups else ""),
                 f"値が変わったセル {len(self.changes)} 件（「変更」タブ）",
                 f"変換前後チェック: {'OK' if self.verification['ok'] else 'NG'}"
                 f"（違反 {s['violations']} / 欠落 {s['lost_cells']}）→「前後チェック」タブ",
                 f"出力フォルダ: {self.out_dir}（変換結果CSV・HTMLレポート・JSONレポート）"]
        lines += [f"書式の内訳 {c['column']}: " + " / ".join(f"{x['label']} {x['count']}件" for x in c["cases"])
                  + f" / 不一致 {c['unmatched']}件" for c in self.coverage]
        for b in r["not_idempotent"]:
            lines.append(f"注意 L102 再実行で値が変わる列 {b['column']}: {b['first']!r} → {b['second']!r}")
        return lines

    def verify_disk(self, csv_path: str, toml_path: str, out_path: str) -> dict:
        """ディスク上の出力CSVを読み直し、入力と設定から作り直した結果と突き合わせる。"""
        self.load(csv_path, toml_path)
        self.verification = verify_files(self.cfg, self.header, self.rows, out_path)
        return self.verification

    def row_view(self, n: int) -> list[str]:
        """n行目（1始まり）の前後を列ごとに並べる。変わった列と新しい列だけ。"""
        uh, _ = uniquify(self.header)
        if not (1 <= n <= len(self.rows)) or n > len(self.out_rows):
            return [f"行番号は 1〜{len(self.rows)} で指定してください"]
        before = dict(zip(uh, self.rows[n - 1] + [""] * len(uh)))
        after = dict(zip(self.out_header, self.out_rows[n - 1]))
        lines = [f"{n}行目  {self.rows[n - 1][0] if self.rows[n - 1] else ''}"]
        for c, v in after.items():
            if c not in before:
                lines.append(f"  ＋ {c}: {v!r}")
            elif before[c] != v:
                lines.append(f"  ～ {c}: {before[c]!r} → {v!r}")
        return lines if len(lines) > 1 else lines + ["  （この行で変わった値はありません）"]

    def shapes(self, csv_path: str, toml_path: str, all_columns: bool = False) -> list[tuple]:
        """形の集計（数値を # にしたもの）。行 = (列, 形の種類数, 件数, 形, 例)。"""
        try:
            self.load(csv_path, toml_path)
        except ConfigError:
            self.load(csv_path, "")           # 設定が未完成でも、形の集計は見られるようにする
        rows = []
        for o in survey_overview(self.cfg, self.header, self.rows, top=50):
            if not all_columns and not (o["shape_count"] > 1 and any("#" in x["shape"] for x in o["shapes"])):
                continue
            for k, sh in enumerate(o["shapes"]):
                rows.append((o["column"] if k == 0 else "", o["shape_count"] if k == 0 else "", sh["count"],
                             sh["shape"], " / ".join(sh["examples"])))
        return rows

    def group_detail(self, n: int) -> list[str]:
        """問題の内訳 n 番目（0始まり）の説明と修正案。"""
        if not (0 <= n < len(self.groups)):
            return []
        g = self.groups[n]
        sg = g["suggestion"]
        lines = [f"{g['count']} 件  {g['code']}  列 {g['column']}" + (f" → {g['out']}" if g["out"] else "")
                 + (f"  形 {g['shape']}" if g["shape"] else ""),
                 f"例: {' / '.join(g['examples'])}    行: {', '.join(map(str, g['rows']))}",
                 f"内容: {g['message']}", "",
                 f"直し方（確度 {sg['confidence']}）: {sg['note']}"]
        if sg["toml"]:
            lines += ["", "---- 設定に入れる案（確認してから使う） ----", sg["toml"].rstrip()]
        return lines

    def draft(self, csv_path: str, toml_path: str) -> str:
        self.load(csv_path, toml_path)
        return draft_toml(self.cfg, self.header, self.rows)

    def example_value(self, column: str) -> str:
        uh, _ = uniquify(self.header)
        if column not in uh:
            return ""
        i = uh.index(column)
        for r in self.rows:
            if i < len(r) and r[i].strip():
                return r[i]
        return ""

    def try_value(self, toml_path: str, column: str, value: str) -> dict:
        cfg = self.cfg or load_config(toml_path)
        return try_row(cfg, {column: value})

    def source_columns(self) -> list[str]:
        return sorted({f.src for f in self.cfg.fields}) if self.cfg else []


def main() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    m = Model()
    root = tk.Tk()
    root.title(f"Tenugui {__version__} — chozu出力の変換")
    root.geometry("1100x720")

    v_in, v_toml, v_out = tk.StringVar(), tk.StringVar(), tk.StringVar()
    v_verify = tk.BooleanVar(value=True)
    v_filter = tk.StringVar(value="すべて")
    v_allcols = tk.BooleanVar(value=False)

    top = ttk.Frame(root, padding=6)
    top.pack(fill="x")

    def row(r: int, label: str, var: tk.StringVar, browse):
        ttk.Label(top, text=label, width=10).grid(row=r, column=0, sticky="w")
        ttk.Entry(top, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4, pady=1)
        ttk.Button(top, text="参照", command=browse).grid(row=r, column=2)

    top.columnconfigure(1, weight=1)

    def pick_in():
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("すべて", "*.*")])
        if p:
            v_in.set(p)
            v_out.set(m.default_output(p))   # 入力CSVのあるフォルダの tenugui_out/

    def pick_toml():
        p = filedialog.askopenfilename(filetypes=[("TOML", "*.toml"), ("すべて", "*.*")])
        if p:
            v_toml.set(p)

    def pick_out():
        p = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if p:
            v_out.set(p)

    row(0, "入力CSV", v_in, pick_in)
    row(1, "ルールTOML", v_toml, pick_toml)
    row(2, "出力CSV", v_out, pick_out)
    ttk.Label(top, text="（変換結果CSV・HTMLレポート・JSONレポートを tenugui_out フォルダに出力。無ければ作成）",
              foreground="gray").grid(row=3, column=1, sticky="w")

    bar = ttk.Frame(root, padding=(6, 0))
    bar.pack(fill="x")

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=6, pady=6)

    def make_tree(parent, cols, widths):
        f = ttk.Frame(parent)
        t = ttk.Treeview(f, columns=cols, show="headings")
        for c, w in zip(cols, widths):
            t.heading(c, text=c)
            t.column(c, width=w, anchor="w")
        ys = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        xs = ttk.Scrollbar(f, orient="horizontal", command=t.xview)
        t.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        t.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        f.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        return f, t

    # 概要
    tab_log = ttk.Frame(nb)
    log = scrolledtext.ScrolledText(tab_log, wrap="word", state="disabled")
    log.pack(fill="both", expand=True)
    nb.add(tab_log, text="概要")

    def say(lines, clear=True):
        log.configure(state="normal")
        if clear:
            log.delete("1.0", "end")
        log.insert("end", "\n".join(lines) + "\n")
        log.configure(state="disabled")

    # 形（ばらつきの把握）
    tab_shape = ttk.Frame(nb)
    sbar = ttk.Frame(tab_shape, padding=4)
    sbar.pack(fill="x")
    ttk.Label(sbar, text="数値を # に置き換えた「形」ごとの件数。形はそのまま pattern に書けます").pack(side="left")
    f_shape, t_shape = make_tree(tab_shape, ("列", "形の種類", "件数", "形", "例"), (240, 70, 60, 260, 420))
    f_shape.pack(fill="both", expand=True)
    nb.add(tab_shape, text="形")

    # 問題
    tab_iss = ttk.Frame(nb)
    fbar = ttk.Frame(tab_iss)
    fbar.pack(fill="x")
    ttk.Label(fbar, text="表示:").pack(side="left")
    cb = ttk.Combobox(fbar, textvariable=v_filter, values=["すべて", "エラーのみ", "警告のみ"],
                      state="readonly", width=12)
    cb.pack(side="left", padx=4)
    f_iss, t_iss = make_tree(tab_iss, ("sev", "code", "row", "key", "column", "message", "raw"),
                             (40, 55, 50, 110, 170, 380, 220))
    f_iss.pack(fill="both", expand=True)
    nb.add(tab_iss, text="問題")

    def fill_issues(*_):
        t_iss.delete(*t_iss.get_children())
        for i in m.issues:
            if v_filter.get() == "エラーのみ" and i["severity"] != "E":
                continue
            if v_filter.get() == "警告のみ" and i["severity"] != "W":
                continue
            t_iss.insert("", "end", values=(i["severity"], i["code"], i["row"], i["key"],
                                            i["out"] or i["column"], i["message"], i["raw"]))

    cb.bind("<<ComboboxSelected>>", fill_issues)

    # 内訳と修正案
    tab_grp = ttk.Frame(nb)
    f_grp, t_grp = make_tree(tab_grp, ("件数", "コード", "列", "形", "例", "案", "確度"),
                             (50, 55, 220, 200, 260, 110, 60))
    f_grp.pack(fill="both", expand=True)
    ttk.Label(tab_grp, text="行を選ぶと、内容と設定の修正案が下に出ます（案は推測です。入れたら「検証」してください）"
              ).pack(anchor="w", padx=4)
    grp_txt = scrolledtext.ScrolledText(tab_grp, height=11)
    grp_txt.pack(fill="x", padx=4, pady=2)
    nb.add(tab_grp, text="内訳と修正案")
    KIND = {"add_case": "case を足す", "to_case": "case 形式に直す", "to_pattern": "pattern で分ける",
            "use_to": "to で換算", "add_strip_unit": "単位を足す", "add_default_unit": "default_unit",
            "add_synonym": "同義語を足す", "none": "—"}

    def fill_groups():
        t_grp.delete(*t_grp.get_children())
        for n, g in enumerate(m.groups):
            t_grp.insert("", "end", iid=str(n), values=(
                g["count"], g["code"], g["column"] + (f" → {g['out']}" if g["out"] else ""), g["shape"],
                " / ".join(g["examples"]), KIND.get(g["suggestion"]["kind"], g["suggestion"]["kind"]),
                g["suggestion"]["confidence"]))
        grp_txt.delete("1.0", "end")

    def show_group(*_):
        sel = t_grp.selection()
        if sel:
            grp_txt.delete("1.0", "end")
            grp_txt.insert("end", "\n".join(m.group_detail(int(sel[0]))) + "\n")

    t_grp.bind("<<TreeviewSelect>>", show_group)

    # 変更
    f_chg, t_chg = make_tree(nb, ("row", "key", "column", "before", "after"), (50, 120, 200, 260, 260))
    nb.add(f_chg, text="変更")
    # プレビュー
    f_prev = ttk.Frame(nb)
    nb.add(f_prev, text="出力プレビュー")
    prev_holder = {"tree": None}

    def fill_preview():
        for w in f_prev.winfo_children():
            w.destroy()
        f, t = make_tree(f_prev, m.out_header or ("-",), [110] * max(1, len(m.out_header)))
        f.pack(fill="both", expand=True)
        for r in m.out_rows[:PREVIEW_ROWS]:
            t.insert("", "end", values=r)
        prev_holder["tree"] = t

    # 試し打ち
    tab_try = ttk.Frame(nb, padding=6)
    nb.add(tab_try, text="試し打ち")
    v_col, v_val = tk.StringVar(), tk.StringVar()
    ttk.Label(tab_try, text="列").grid(row=0, column=0, sticky="w")
    cb_col = ttk.Combobox(tab_try, textvariable=v_col, width=36)
    cb_col.grid(row=0, column=1, sticky="w", padx=4)
    ttk.Label(tab_try, text="値").grid(row=1, column=0, sticky="w")
    ttk.Entry(tab_try, textvariable=v_val, width=60).grid(row=1, column=1, sticky="w", padx=4, pady=2)
    out_try = scrolledtext.ScrolledText(tab_try, height=22, state="disabled")
    out_try.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=6)
    tab_try.rowconfigure(3, weight=1)
    tab_try.columnconfigure(2, weight=1)

    def try_say(lines):
        out_try.configure(state="normal")
        out_try.delete("1.0", "end")
        out_try.insert("end", "\n".join(lines) + "\n")
        out_try.configure(state="disabled")

    def example():
        if v_col.get():
            v_val.set(m.example_value(v_col.get()))

    def run_try():
        try:
            if not v_toml.get():
                raise ConfigError(["ルールTOMLを指定してください"])
            m.cfg = load_config(v_toml.get())     # 編集直後でも最新のルールで試す
            res = m.try_value(v_toml.get(), v_col.get(), v_val.get())
        except ConfigError as e:
            try_say(["設定エラー:"] + [f"  - {p}" for p in e.problems])
            return
        lines = []
        for col, tr in res["trace"].items():
            lines.append(f"[{col}]")
            lines += [f"   {t['stage']:10s} {t['value']}" for t in tr]
        lines.append("出力: " + str(res["outputs"]))
        lines += [f"  {i['severity']} {i['code']}: {i['message']}  → {i['hint']}" for i in res["issues"]]
        try_say(lines)

    ttk.Button(tab_try, text="入力CSVから例を入れる", command=example).grid(row=0, column=2, sticky="w")
    ttk.Button(tab_try, text="試す", command=run_try).grid(row=2, column=1, sticky="w", padx=4)

    # --- ボタン
    def guarded(fn):
        def inner():
            try:
                if not v_in.get():
                    messagebox.showwarning("Tenugui", "入力CSVを指定してください")
                    return
                fn()
            except ConfigError as e:
                say(["エラー:"] + [f"  - {p}" for p in e.problems])
                nb.select(tab_log)
            except Exception:
                say(["予期しないエラー:", traceback.format_exc()])
                nb.select(tab_log)
        return inner

    # 前後チェック
    tab_ver = ttk.Frame(nb)
    nb.add(tab_ver, text="前後チェック")
    vbar = ttk.Frame(tab_ver, padding=4)
    vbar.pack(fill="x")
    v_sum = tk.StringVar(value="変換すると、ここに前後の突き合わせ結果が出ます")
    lbl_sum = ttk.Label(vbar, textvariable=v_sum, font=("", 11, "bold"))
    lbl_sum.pack(side="left")
    f_cols, t_cols = make_tree(tab_ver, ("列", "ルール対象", "変更", "欠落", "エラー", "例（前 → 後）"),
                               (220, 80, 60, 60, 60, 520))
    f_cols.pack(fill="both", expand=True)
    ttk.Label(tab_ver, text="違反（あってはならない変化）").pack(anchor="w", padx=4)
    f_vio, t_vio = make_tree(tab_ver, ("種類", "列", "行", "前", "後"), (140, 200, 50, 300, 300))
    f_vio.pack(fill="both", expand=True)
    rbar = ttk.Frame(tab_ver, padding=4)
    rbar.pack(fill="x")
    v_rowno = tk.StringVar(value="1")
    ttk.Label(rbar, text="行を指定して前後を見る:").pack(side="left")
    ttk.Entry(rbar, textvariable=v_rowno, width=6).pack(side="left", padx=4)
    row_txt = scrolledtext.ScrolledText(tab_ver, height=7, state="disabled")

    def show_row():
        try:
            lines = m.row_view(int(v_rowno.get()))
        except ValueError:
            lines = ["行番号は整数で入力してください"]
        row_txt.configure(state="normal")
        row_txt.delete("1.0", "end")
        row_txt.insert("end", "\n".join(lines) + "\n")
        row_txt.configure(state="disabled")

    ttk.Button(rbar, text="見る", command=show_row).pack(side="left")
    row_txt.pack(fill="x", padx=4, pady=2)

    VIO = {"row_count": "行数が変わった", "untouched_changed": "対象外の列が変わった",
           "ng_altered": "抽出NG印が変わった", "column_missing": "列が消えた",
           "output_differs": "出力が設定と食い違う（古い/手編集）"}

    def fill_verify():
        v = m.verification
        if not v:
            return
        s = v["summary"]
        v_sum.set(f"{'OK' if v['ok'] else 'NG'}  行 {s['rows_in']}→{s['rows_out']} / 変更 {s['changed_cells']} / "
                  f"欠落 {s['lost_cells']} / エラー {s['error_cells']} / 違反 {s['violations']}")
        t_cols.delete(*t_cols.get_children())
        for c in v["columns"]:
            ex = "  /  ".join(f"{a!r} → {b!r}" for a, b in c["sample"])
            t_cols.insert("", "end", values=(c["column"], "○" if c["ruled"] else "", c["changed"],
                                             c["lost"], c["errors"], ex))
        for d in v.get("dropped_columns", []):
            t_cols.insert("", "end", values=(d["column"] + "（削除）", "", "", "", d["failed_rows"] or "",
                                             "元の列を削除" + (f"（変換失敗 {d['failed_rows']} 行の元の値は HTML / JSON レポートに記録）"
                                                              if d["failed_rows"] else "")))
        for n in v["new_columns"]:
            t_cols.insert("", "end", values=(n["column"] + "（新規）", "", "", "", "",
                                             f"{n['filled']}/{n['rows']} 行に値"))
        t_vio.delete(*t_vio.get_children())
        for x in v["violations"]:
            t_vio.insert("", "end", values=(VIO.get(x["kind"], x["kind"]), x["column"], x["row"],
                                            x["before"], x["after"]))

    def do_verify_disk():
        if not v_out.get():
            messagebox.showwarning("Tenugui", "出力CSVを指定してください")
            return
        if not Path(v_out.get()).exists():
            messagebox.showwarning("Tenugui", "出力CSVがまだありません。先に「変換」してください")
            return
        m.verify_disk(v_in.get(), v_toml.get(), v_out.get())
        fill_verify()

    ttk.Button(vbar, text="出力CSVを読み直して検証", command=guarded(do_verify_disk)).pack(side="right")

    def refresh_cols():
        cb_col.configure(values=m.source_columns())

    def do_check():
        say(m.check(v_in.get(), v_toml.get()))
        refresh_cols()
        nb.select(tab_log)

    def do_convert():
        if not v_out.get():
            messagebox.showwarning("Tenugui", "出力CSVを指定してください")
            return
        lines = m.convert(v_in.get(), v_toml.get(), v_out.get(), v_verify.get())
        say(lines)
        fill_issues()
        fill_groups()
        t_chg.delete(*t_chg.get_children())
        for c in m.changes:
            t_chg.insert("", "end", values=(c["row"], c["key"], c["column"], c["before"], c["after"]))
        fill_preview()
        fill_verify()
        refresh_cols()
        nb.select(tab_ver if not m.verification["ok"] else (tab_grp if m.groups else tab_log))

    def do_shapes():
        rows = m.shapes(v_in.get(), v_toml.get(), v_allcols.get())
        t_shape.delete(*t_shape.get_children())
        for r in rows:
            t_shape.insert("", "end", values=r)
        refresh_cols()
        nb.select(tab_shape)

    ttk.Checkbutton(sbar, text="全ての列", variable=v_allcols).pack(side="right")
    ttk.Button(sbar, text="集計する", command=guarded(do_shapes)).pack(side="right", padx=4)

    def do_draft():
        text = m.draft(v_in.get(), v_toml.get())
        dd = default_out_dir(v_in.get())
        dd.mkdir(parents=True, exist_ok=True)
        p = filedialog.asksaveasfilename(defaultextension=".toml", filetypes=[("TOML", "*.toml")],
                                         initialdir=str(dd), initialfile="draft.toml")
        if p:
            Path(p).write_text(text, encoding="utf-8")
            say([f"下書きを書きました → {p}", "内容を確認・編集してから、ルールTOMLに指定して「検証」してください。"])
            v_toml.set(p)

    ttk.Button(bar, text="形を集計", command=guarded(do_shapes)).pack(side="left", padx=2)
    ttk.Button(bar, text="検証", command=guarded(do_check)).pack(side="left", padx=2)
    ttk.Button(bar, text="変換", command=guarded(do_convert)).pack(side="left", padx=2)
    ttk.Button(bar, text="下書きを作る", command=guarded(do_draft)).pack(side="left", padx=2)
    ttk.Checkbutton(bar, text="再実行の安定性も確認", variable=v_verify).pack(side="left", padx=10)

    def open_out_dir():
        d = m.out_dir or (str(default_out_dir(v_in.get())) if v_in.get() else "")
        if not d or not Path(d).exists():
            messagebox.showinfo("Tenugui", "出力フォルダはまだありません（変換すると作られます）")
            return
        import os
        import subprocess
        import sys
        if hasattr(os, "startfile"):
            os.startfile(d)                                   # Windows
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", d])

    def open_report():
        if not m.html_path or not Path(m.html_path).exists():
            messagebox.showinfo("Tenugui", "レポートはまだありません（変換すると作られます）")
            return
        import webbrowser
        webbrowser.open(Path(m.html_path).resolve().as_uri())

    ttk.Button(bar, text="出力フォルダを開く", command=open_out_dir).pack(side="right", padx=2)
    ttk.Button(bar, text="レポートを開く", command=open_report).pack(side="right", padx=2)

    say(["1. 入力CSV（chozuの出力）を選ぶ",
         "2. 「形を集計」で、ばらつきのある列と値の形を見る（ルールTOMLが無くても使えます）",
         "3. ルールTOMLを選ぶ。まだ無ければ「下書きを作る」でCSVの実態から作れます",
         "4. 「検証」→「変換」。問題は「内訳と修正案」タブに種類ごとにまとまり、行を選ぶと直し方の案が出ます",
         "5. 「レポートを開く」で、色付きのHTMLレポートをブラウザで見られます",
         "6. 値の調整は「試し打ち」タブで1つずつ",
         "", "設定の書き方: コマンドで tenugui schema"])
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
