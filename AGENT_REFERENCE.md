# Tenugui AGENT_REFERENCE（agent向け・機械向けの仕様）

動機や比喩は書かない。人向けは README.md。バージョン 0.6.0 / schema_version = 1。

## 0. 不変条件
- I1 決定的: 同じ入力・同じ設定なら同じ出力。乱数・時刻・ネットワークを使わない
- I2 黙って通さない: 換算不能・次元違い・未定義単位・数値として読めない値は、必ず `C1xx` として報告する。値は `on_error` に従い「元の値を残す(keep)/空にする(empty)」
- I3 別名列(`out` ≠ `src`)の失敗は常に空。元の値は src 列に残る
- I4 chozuの抽出NG印（`settings.reserved` の正規表現、既定 `^NG\d*$`）は一切変換せず、そのまま通す。報告もしない
- I5 空セルは空のまま通す（`required = true` のときだけ C201）
- I6 派生値は、入力が空→空 / NG印→空+W(C302) / 上流で変換失敗→空（二重報告しない）
- I7 重複列名は `名前#2` `名前#3` に一意化して出力する

## 1. 1つの [[field]] の処理順
`reserved?` → 空? → NFKC正規化+strip → `sub` → `synonyms` → `match/extract` → `to`(数値+単位パース→換算) → `decimals/trim/unit_out`
- NFKC: 全角→半角、`㎜`→`mm`、`℃`→`°C`、`～`→`~`、`²`→`2`、`µ`→`μ`。**正規表現はNFKC後の文字に対して書く**（`～` ではなく `[~～]`、`mm²` ではなく `mm2`）
- 数値より前に文字があるもの(`約85mm`, `φ10`)は C101。`sub` で落とすか `match` で数値部分だけ取り出す
- 数値のあとの残り全部を単位として読む。`4(二重)` は C103。`match` で分ける

## 2. キー表
`[settings]` `encoding_in`(utf-8-sig) `encoding_out`(utf-8-sig) `passthrough`(true) `keep`([]) `reserved`(['^NG\d*$']) `on_error`(keep|empty)

`[units.<次元>]` `base`=基準単位、`単位 = 倍率`（文字列可: `inch = "25.4"`）。式定義: `"°C" = { to_base = "x + 273.15", from_base = "x - 273.15" }`（変数は x のみ、base は倍率で定義された単位）
既定の次元: length mm / voltage V / current A / resistance Ω / power W / mass g / time s / pressure Pa / frequency Hz / ratio % / temperature K(°C, °F は式)
`[unit_alias]` `"ミリ" = "mm"`
`[synonyms.<名>]` `正規名 = [ゆれ...]`（突合は NFKC・大小無視・空白と `-_・` を除去）

`[[field]]`（通常）`number_only`+`strip_units`(number_only=true のとき strip_units=[捨ててよい単位...] 必須。先頭の数値だけ取り、strip_units にある単位は換算せず捨てる。単位なしの値は通す。それ以外の単位は C104。`["*"]` は何でも捨てる。`to` `default_unit` `unit_out` `unit_label` とは併用不可) `src` `out`(=src: 置換 / ≠src: 元列の直後に追加) `drop_src` `required` `sub`=[[regex,repl],...] `synonyms` `on_unknown`(warn|keep|error) `extract`(group 1 を採用) `to` `default_unit` `unit_out`(strip|add) `unit_label` `decimals` `trim`
`[[field]]`（preset分解）`src` `preset`(tolerance|range) `parts`={ 部品名 = { out, to, default_unit, ...通常キー } } `drop_src`
- tolerance: 部品 `nom` `tol` `hi` `lo`。形: `100±5V` `100V±5V` `100V±5` `100+5/-3V` `φ13±0.2` `100V`（許容差なし→tol は空）。先頭の `φΦØ` は無視
- range: 部品 `lo` `hi`。形: `11.5~13.5A` `5A~10A` `500mA~1.2A`（`～`はNFKCで`~`になる。`-`区切りは負数と区別できないため非対応）
- `to` を付けた部品は、同じ値の中の単位を共有する（nom は先頭側の単位、tol/hi/lo と range の hi は末尾側の単位を優先）。`to` なしの部品は数値テキストのみ（単位は捨てる）
- tolerance_range: 部品 `nom` `tol` `hi` `lo` `rlo` `rhi`。tolerance に括弧内の範囲を足した形。`1250W±100W(11.5~13.5A)` `1250W(11.5~13.5A)` `1250W±100W`。nom/tol の単位は tolerance と同じ規則、rlo は範囲の前側の単位→後ろ側、rhi は後ろ側→前側を優先
- 括弧つき・複数の単位・独自形式は `match`+`parts`

`[[field]]`（分解）`src` `match`(名前付きグループ) `parts`={ グループ名 = { out, to, default_unit, unit_group, ...通常キー } } `drop_src`
- `unit_group = "u"` : 別グループ u の文字列を単位として数値に連結する（`11.5~13.5A` の A を lo/hi で共有）
- 省略可能なグループ `(?:...)?` が不一致 → その part は空（`required = true` のときだけ C201）。`match` 自体が不一致 → C111 を1件だけ報告し、全 part は空
- `to` を書いた part/field だけが数値化される。`unit_group` `default_unit` `unit_out` `unit_label` は `to` が必要

`[[derive]]` `out` `expr` `vars`={ 変数 = "列名" または "列名@単位" } `unit`(式の結果の単位) `to`(unit からこの単位へ換算) `unit_out`(strip|add) `decimals` `trim` `after`
- 宣言順に評価。後ろで定義される列は参照不可。**派生は丸め前の値で計算する**（`decimals` は出力の表記だけに効く）
- 列名が field の出力なら、その値(`to` の単位)を使う。`@単位` を付けるとその単位へ換算してから使う
- 生の列は、単位なし→数値(`@`があれば C304 を警告して単位を仮定)、単位あり→`@単位` が無いと C311
- 式: 数値・変数・`+ - * / **`(整数指数,|n|≤64)・括弧・`pi`・`abs min max round sqrt`。それ以外は設定エラー

## 3. 単位式
`mm` `mm^2` `mm2` `mm²` `mm*mm` `m/s` `mm/min` `kg*m/s^2` を解釈。次元の指数が一致すれば換算できる（`mm^2`↔`cm2`）。式定義の単位は単独のみ（`W/°C` は不可）

## 3b. 出力ファイル（convert）
既定の出力先 = 入力CSVのあるフォルダの `tenugui_out/`（無ければ作成）。`--out-dir` で変更、`-o` で出力CSV名、`--timestamp` で `_YYYYmmdd_HHMMSS` 付き。
`<名>_tenugui.csv`（結果）`<名>_tenugui_conv.csv`（報告）`<名>_tenugui_diff.csv`（変更セル。`--no-diff` で省略）`<名>_tenugui.log`（実行ログ: コマンド・設定sha・前後チェック・問題一覧）

## 3b2. 元の列の削除 `drop_src`
`drop_src = true`（field / preset / match の行）で、元の列を出力から消す。`out = src`（元の列への置換）との併用は設定エラー。消した列は verification.dropped_columns:[{column, failed_rows}] に出る（意図した削除なので違反にはならない）。失敗行の元の値は *_conv.csv の raw とログに残る。派生の vars は消した列も参照できる

## 3c. 変換前後チェック（convert で毎回自動 / `verify IN OUT -c R`）
violations（1件でもあれば ok=false。convert は `--fail-on` で終了コードに反映）:
`row_count` 行数が変わった / `untouched_changed` ルール対象外の列（変換しない列・out違いの元列）が変わった / `ng_altered` 抽出NG印が変わった / `column_missing` 入力列が意図なく消えた / `output_differs`（verifyのみ）ディスク上の出力が、入力+設定から作り直した結果と違う
warning相当: `summary.lost_cells`（値があったセルが空になった）。`columns[]` は列ごとの changed/lost/errors/sample、`new_columns[]` は新規列の埋まり具合。
JSON: `convert` の `verification:{ok,summary,violations[]}`、`verify` は `{schema:"tenugui.verify/1", ok, summary, violations[], columns[], new_columns[]}`

## 4. 報告コード（変換レポート・JSONの issues[].code）
| code | sev | 原因 | 直し方 |
|---|---|---|---|
| C001 | E | 設定の src/vars/after/keep が入力に無い | 列名を直す（chozu側の列名変更も疑う） |
| C002 | E | 出力列名が入力列と衝突 | out を変える |
| C101 | E | 数値+単位として読めない | match/parts, extract, sub で数値部分だけ取る |
| C102 | E | 単位が無く default_unit も無い | default_unit を足す |
| C103 | E | 未定義の単位 / 次元違い | [units.*]・[unit_alias] に追加、または to を直す |
| C104 | E | number_only で strip_units に無い単位 | strip_units に加える、または to で換算する |
| C111 | E | match/extract 不一致 | `try` で正規表現を試す（NFKC後の文字で書く） |
| C121 | W/E | 同義語表に無い | [synonyms.*] に追加（on_unknown=error のとき E） |
| C201 | E | 必須が空 | 入力の欠落、または match の該当部分なし |
| C301 | E | 派生の入力が数値として読めない | 上流 field で数値化 |
| C302 | W | 派生の入力がNG印 | 情報のみ |
| C303 | E | 0除算など | 式か入力を確認 |
| C304 | W | 単位なしを vars の単位とみなした | 入力に単位があるか確認 |
| C305 | E | 派生の換算不能 | unit/to/vars の次元を揃える |
| C311 | E | 単位付き生値に `@単位` なし | vars を `列@単位` にする |
| L101 | W(lint) | 置換列が単位なし出力で default_unit なし → 再実行で C102 | default_unit を足す |
| L102 | W | `--verify`/`--idempotent` で再実行すると値が変わる列 | 設定を直す |
| L201 | W(lint) | 未使用の同義語表 | 削除または field から参照 |
| C003 | W | lock と列構成が違う（増/減） | 意図した変更なら `tenugui lock` で再記録 |

## 5. CLI と JSON（`--format json` は stdout に1行のJSON）
終了コード: 0 OK / 1 `--fail-on` 基準に該当 / 2 設定・使い方のエラー（JSON時も `{"schema":"tenugui.error/1","ok":false,"config_errors":[{where,message}]}` を stdout に出す）
- `tenugui check -c R [IN] [--idempotent] [--format json] [--fail-on error|warn|none(既定error)]` → `{schema:"tenugui.check/1", ok, fields, derives, input_problems[], lint[], duplicate_columns[], header_drift, not_idempotent[]}`
- `tenugui convert IN -c R [-o OUT] [--out-dir D] [--timestamp] [--no-diff] [--verify] [--max-issues N] [--format json] [--fail-on ...(既定none)]` → OUT, OUT_conv.csv(row,key,column,code,severity,message,raw,out), `--diff` で OUT_diff.csv(row,key,column,before,after)。JSON: `{schema:"tenugui.convert/1", ok, rows, counts:{error,warning}, files, issues[{row,key,column,out,code,severity,message,raw,hint}], issues_truncated, changed_cells, not_idempotent[], lint[], header:{sha,duplicates,drift}}`
- `tenugui try -c R --field 列 値 [--set 列=値 ...]` → CSVなしで1行試す。`{schema:"tenugui.try/1", ok, outputs{}, issues[], trace{出力列:[{stage,value}]}, skipped_fields[], skipped_derives[]}`。stage: normalize / sub / synonyms / extract / quantity / format / reserved
- `tenugui survey IN -k 列 [-k 列...] [-c R] --format json` → `{columns:[{column,distinct,top_values[{value,count}],units{},unparsable,candidates[{canonical,variants[],kind:exact|similar,count}],composite_examples[]}]}`
- `tenugui verify IN OUT_CSV -c R [--format json] [--fail-on ...(既定error)]`
- `tenugui draft IN [-c R] [-o draft.toml(既定 tenugui_out/draft.toml)] [--stdout]` → 実態から作る下書き。exact な表記ゆれと、単位付き数値列(80%以上が読める)だけを出し、疑い(similar)は `#` コメントにする。**下書きは必ず `check` と `try` で検証してから使う**
- `tenugui lock IN -c R` → R.lock.json に列構成を記録。以後 check/convert が列の増減を C003 で報告

## 6. 推奨ループ（agent）
1. `survey ... --format json` で列の実態（単位・複合表記・表記ゆれ候補）を見る / または `draft`
2. TOML を書く（複合表記は `match`+`parts`、正規表現は TOML の**リテラル文字列 `'...'`**で書く。`"\d"` は構文エラー）
3. `check -c R IN --format json --idempotent` → `config_errors` / `input_problems` / `lint` を直す
4. 難しい値は `try --field 列 値 --format json` で `trace` を見て段階ごとに直す
5. `convert IN -c R -o OUT --diff --verify --format json` → `issues[].hint` に従い直す。`changed_cells` と `OUT_diff.csv` で「意図した変更だけか」を確認する
6. 本番の列構成が固まったら `lock`
7. 完了条件: `counts.error == 0`、`not_idempotent == []`、`header.drift` が null か changed=false

## 6b. 「A,B,CならA」
- 値の統一（B・C も A）: `[synonyms.g]` に `"A" = ["B", "C"]`、field に `synonyms = "g"`。突合は完全一致（NFKC・大小無視・空白と `-_・` を除去）。部分一致はしない。表に無い値は `on_unknown`（warn/keep/error）
- セル内の並び（`A,B,C` の先頭）: `extract = '^([^,、]+)'`。最後なら `extract = '([^,、]+)$'`

## 7. 落とし穴
- TOML の通常文字列で `"\d"` `"\s"` は構文エラー → `'\d'`
- TOML 1.0 はインラインテーブル内の改行不可。`parts = { ... }` は1行で書く（長ければ `[field.parts.x]` サブテーブル形式でも可）
- `[synonyms.x]` を作ったら field から `synonyms = "x"` で参照する（未参照は L201）
- 数字だけ違う値（型番・番号・寸法）は同義語として束ねない（draft/survey も束ねない）
- 実データ由来の列名・値・型式名は共有物（README・examples・テスト）に入れない
