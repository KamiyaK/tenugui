"""訂正・保留（ルールTOMLとは別のファイル）と、変換の来歴の記録。

ルールTOML（表記ゆれの統一・単位）は「意味を変えない言い換え」で、ずっと使う辞書。
ここで扱うのは元データそのものの問題で、元データが直れば消えるべきもの:

  [[correct]]  訂正。正解が自明な誤記。ルールにかける前に値を置き換える。理由は必須
  [[hold]]     保留。判断が要る値。変換せず元の値のまま通し、C141 で報告する。理由は必須

どちらも「どの値を、なぜ」を残すので、そのまま元データの修正依頼リストになる。
ファイルは ルールTOML と同じ場所の <ルール名>.corrections.toml（あれば自動で読む）。
"""
from __future__ import annotations

import difflib
import hashlib
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .units import normalize_text

CORRECTIONS_SCHEMA_VERSION = 1
CORRECT_KEYS = {"column", "value", "to", "reason", "key", "ref"}
HOLD_KEYS = {"column", "value", "reason", "key", "ref"}
_TOP_KEYS = {"schema_version", "correct", "hold"}
KIND_LABEL = {"correct": "訂正", "hold": "保留", "normalize": "表記統一"}


@dataclass(frozen=True)
class Entry:
    kind: str                 # "correct" / "hold"
    where: str                # [[correct]]#2 のような場所（報告用）
    column: str
    value: str                # 元の値（書かれたまま）
    to: str                   # 訂正後（hold では空）
    reason: str
    keys: tuple[str, ...]     # 先頭列（シート名など）の絞り込み。空 = その列の全行
    ref: str = ""


@dataclass
class Corrections:
    entries: list[Entry] = field(default_factory=list)
    path: str = ""
    sha: str = ""
    # 列 -> 正規化した値 -> {正規化したキー（"" は全行）: Entry}
    index: dict[str, dict[str, dict[str, Entry]]] = field(default_factory=dict)

    @property
    def columns(self) -> list[str]:
        return list(self.index)

    def lookup(self, column: str, raw: str, key: str) -> Entry | None:
        """この値に当たる訂正・保留。キーを指定したものが、列全体のものより優先。"""
        by_val = self.index.get(column)
        if not by_val:
            return None
        by_key = by_val.get(normalize_text(raw))
        if not by_key:
            return None
        return by_key.get(normalize_text(key)) or by_key.get("")


@dataclass
class Provenance:
    """変換1回ぶんの来歴。convert_rows に渡すと埋まる。"""
    corrected: dict[tuple[int, str], tuple[str, Entry]] = field(default_factory=dict)   # (行, 列) -> (元の値, 訂正)
    held: dict[tuple[int, str], tuple[str, Entry]] = field(default_factory=dict)        # (行, 列) -> (元の値, 保留)
    keys: dict[int, str] = field(default_factory=dict)                                  # 行 -> 先頭列の値
    hits: Counter = field(default_factory=Counter)                                      # Entry.where -> 当たった回数
    syn: Counter = field(default_factory=Counter)             # (同義語表, 正式名, 実際の表記) -> 件数
    syn_cols: dict[str, set] = field(default_factory=dict)    # 同義語表 -> 使った列


def corrections_path(config_path: str | Path) -> Path:
    """ルールTOML r.toml に対する訂正ファイルの既定の場所: r.corrections.toml"""
    return Path(config_path).with_suffix(".corrections.toml")


def _near(word: str, candidates) -> str:
    hit = difflib.get_close_matches(word, [str(c) for c in candidates], n=3, cutoff=0.6)
    return f"（もしかして: {', '.join(hit)}）" if hit else ""


def parse_corrections(raw: dict, is_reserved: Callable[[str], bool] = lambda s: False,
                      ) -> tuple[Corrections, list[str]]:
    """辞書（TOMLを読んだもの）を検証して Corrections にする。戻り値 (結果, 問題点)。"""
    p: list[str] = []
    for k in raw:
        if k not in _TOP_KEYS:
            p.append(f"訂正ファイル: 未知のキー {k!r}{_near(str(k), _TOP_KEYS)}"
                     "（書けるのは schema_version / [[correct]] / [[hold]]）")
    if raw.get("schema_version") != CORRECTIONS_SCHEMA_VERSION:
        p.append(f"訂正ファイル: schema_version = {CORRECTIONS_SCHEMA_VERSION} が必要です")
    cor = Corrections()
    for kind, allowed in (("correct", CORRECT_KEYS), ("hold", HOLD_KEYS)):
        items = raw.get(kind, [])
        if not isinstance(items, list):
            p.append(f"訂正ファイル: {kind} は [[{kind}]] の形で書きます")
            continue
        for n, d in enumerate(items, start=1):
            w = f"[[{kind}]]#{n}"
            if not isinstance(d, dict):
                p.append(f"{w}: column / value / reason を持つ表で書きます")
                continue
            for k in d:
                if k not in allowed:
                    p.append(f"{w}: 未知のキー {k!r}{_near(str(k), allowed)}（使えるキー: {', '.join(sorted(allowed))}）")
            ok = True
            need = ("column", "value", "to", "reason") if kind == "correct" else ("column", "value", "reason")
            for k in need:
                if not isinstance(d.get(k), str) or not normalize_text(d[k]):
                    what = {"column": "入力の列名", "value": "元の値", "to": "訂正後の値",
                            "reason": "理由（なぜ訂正・保留するのか。後から読む人のために必須）"}[k]
                    p.append(f"{w}: {k} が必要です（{what}。空でない文字列）")
                    ok = False
            keys = d.get("key", [])
            if isinstance(keys, str):
                keys = [keys]
            if not isinstance(keys, list) or not all(isinstance(x, str) and normalize_text(x) for x in keys):
                p.append(f"{w}: key は \"A001\" か [\"A001\", \"A002\"] の形で書きます（先頭列の値。省略すると列の全行）")
                ok = False
            if "ref" in d and not isinstance(d["ref"], str):
                p.append(f"{w}: ref は文字列で書きます（根拠・照会先）")
                ok = False
            if not ok:
                continue
            if kind == "correct" and normalize_text(d["value"]) == normalize_text(d["to"]):
                p.append(f"{w}: value と to が同じです")
                continue
            if is_reserved(d["value"]):
                p.append(f"{w}: 抽出NG印 {d['value']!r} は訂正・保留の対象にできません（NG印はそのまま通します）")
                continue
            e = Entry(kind, w, d["column"], d["value"], d.get("to", "") if kind == "correct" else "",
                      d["reason"].strip(), tuple(keys), d.get("ref", "").strip())
            slot = cor.index.setdefault(e.column, {}).setdefault(normalize_text(e.value), {})
            for k in (e.keys or ("",)):
                nk = normalize_text(k)
                if nk in slot:
                    scope = f"key = {k!r}" if nk else "列の全行"
                    p.append(f"{w}: {e.column} の {e.value!r}（{scope}）は {slot[nk].where} にもあります"
                             "（同じ値への訂正・保留は1つだけ）")
                else:
                    slot[nk] = e
            cor.entries.append(e)
    # 訂正後の値が、別の訂正・保留の対象になっていないか（再実行で値が変わるのを防ぐ）
    for e in cor.entries:
        if e.kind == "correct":
            other = cor.index.get(e.column, {}).get(normalize_text(e.to))
            if other:
                o = next(iter(other.values()))
                p.append(f"{e.where}: 訂正後の値 {e.to!r} が {o.where} の対象になっています"
                         "（訂正は1回で最終の値にします）")
    return cor, p


def read_corrections(path: str | Path) -> tuple[dict, str, list[str]]:
    """訂正ファイルを読む。戻り値 (辞書, sha, 問題点)。"""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return {}, "", [f"訂正ファイルが見つかりません: {path}"]
    try:
        raw = tomllib.loads(data.decode("utf-8-sig"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return {}, "", [f"訂正ファイルのTOML構文エラー: {e}（{path}）"]
    return raw, hashlib.sha256(data).hexdigest()[:12], []


# ---------------------------------------------------------------- 来歴のまとめ
def fix_cells(prov: Provenance) -> list[dict]:
    """訂正・保留のかかったセルの一覧（行順）。修正依頼リストCSVの中身。"""
    res = []
    for (row, col), (raw, e) in prov.corrected.items():
        res.append({"kind": "correct", "row": row, "key": prov.keys.get(row, ""), "column": col,
                    "before": raw, "after": e.to, "reason": e.reason, "ref": e.ref})
    for (row, col), (raw, e) in prov.held.items():
        res.append({"kind": "hold", "row": row, "key": prov.keys.get(row, ""), "column": col,
                    "before": raw, "after": "", "reason": e.reason, "ref": e.ref})
    return sorted(res, key=lambda d: (d["row"], d["column"]))


def fix_requests(prov: Provenance, max_keys: int = 20) -> list[dict]:
    """訂正・保留を1件（＝ファイルの1エントリ）ごとにまとめる。保留が先、件数の多い順。"""
    by: dict[str, dict] = {}
    for kind, cells in (("correct", prov.corrected), ("hold", prov.held)):
        for (row, col), (raw, e) in sorted(cells.items()):
            g = by.setdefault(e.where, {"kind": kind, "where": e.where, "column": e.column, "value": e.value,
                                        "to": e.to, "reason": e.reason, "ref": e.ref, "count": 0,
                                        "keys": [], "rows": [], "keys_truncated": False})
            g["count"] += 1
            k = prov.keys.get(row, "")
            if k not in g["keys"]:
                if len(g["keys"]) < max_keys:
                    g["keys"].append(k)
                else:
                    g["keys_truncated"] = True
            if len(g["rows"]) < max_keys:
                g["rows"].append(row)
    return sorted(by.values(), key=lambda d: (d["kind"] != "hold", -d["count"], d["column"], d["value"]))


def unused_entries(cor: Corrections | None, prov: Provenance, header: list[str] | None = None) -> list[dict]:
    """一度も当たらなかった訂正・保留。元データが直った（＝消してよい）か、列名・値の書き間違い。"""
    if cor is None:
        return []
    res = []
    for e in cor.entries:
        if prov.hits[e.where]:
            continue
        missing = header is not None and e.column not in header
        res.append({"code": "L301" if e.kind == "correct" else "L302", "kind": e.kind, "where": e.where,
                    "column": e.column, "value": e.value, "to": e.to, "key": list(e.keys), "reason": e.reason,
                    "column_missing": missing,
                    "message": (f"列 {e.column!r} が入力にありません{_near(e.column, header or [])}" if missing else
                                f"{e.column} に {e.value!r} はもうありません。元データが直ったなら、この{KIND_LABEL[e.kind]}は消せます")})
    return res


def glossary_used(prov: Provenance) -> list[dict]:
    """この入力で実際に使われた表記: 同義語表 × 正式名ごとに、どの表記が何件あったか。"""
    by: dict[tuple[str, str], Counter] = {}
    for (sname, canon, variant), n in prov.syn.items():
        by.setdefault((sname, canon), Counter())[variant] += n
    res = []
    for (sname, canon), c in by.items():
        vs = [{"value": v, "count": n, "canonical": v == canon} for v, n in c.most_common()]
        res.append({"set": sname, "canonical": canon, "total": sum(c.values()),
                    "unified": sum(n for v, n in c.items() if v != canon), "variants": vs,
                    "columns": sorted(prov.syn_cols.get(sname, []))})
    return sorted(res, key=lambda d: (d["set"], -d["unified"], d["canonical"]))


def summarize(cor: Corrections | None, prov: Provenance, changes: list[dict], header: list[str],
              report_unused: bool = True) -> dict:
    """レポートに載せる来歴（JSON の provenance）。changes の各要素に kind も付ける。"""
    for ch in changes:
        ch["kind"] = "correct" if (ch["row"], ch["column"]) in prov.corrected else "normalize"
    return {"corrections_file": cor.path if cor else None, "corrections_sha": cor.sha if cor else "",
            "counts": {"normalize": sum(1 for ch in changes if ch["kind"] == "normalize"),
                       "correct": len(prov.corrected), "hold": len(prov.held)},
            "fix_requests": fix_requests(prov),
            "unused": unused_entries(cor, prov, header) if report_unused else [],
            "glossary": glossary_used(prov)}
