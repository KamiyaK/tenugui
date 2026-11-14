# Tenugui AGENT_REFERENCE（agent向け・機械向けの仕様）

動機や比喩は書かない。人向けは README.md。バージョン 0.11.0 / schema_version = 1。

## 0. 不変条件
- I1 決定的: 同じ入力・同じ設定なら同じ出力。乱数・時刻・ネットワークを使わない
- I2 黙って通さない: 換算不能・次元違い・未定義単位・数値として読めない値は、必ず `C1xx` として報告する。値は `on_error` に従い「元の値を残す(keep)/空にする(empty)」
- I3 別名列(`out` ≠ `src`)の失敗は常に空。元の値は src 列に残る
- I4 chozuの抽出NG印（`settings.reserved` の正規表現、既定 `^NG\d*$`）は一切変換せず、そのまま通す。報告もしない
- I5 空セルは空のまま通す（`required = true` のときだけ C201）
- I6 派生値は、入力が空→空 / NG印→空+W(C302) / 上流で変換失敗→空（二重報告しない）
- I7 重複列名は `名前#2` `名前#3` に一意化して出力する
- I8 元データの問題を黙って直さない: 誤記の訂正は訂正ファイルの `[[correct]]`（理由必須）、判断が要る値は `[[hold]]`（変換しない）。どちらも原値・理由つきでレポートに残る。推測で値を決めない

## 1. 1つの [[field]] の処理順
（訂正ファイルがあれば最初に: `[[correct]]` に当たる値は置き換えてから下へ / `[[hold]]` に当たる値は下を一切かけない）
`reserved?` → 空? → NFKC正規化+strip → `sub` → `synonyms` → `match/extract` → `to`(数値+単位パース→換算) → `decimals/trim/unit_out`
- NFKC: 全角→半角、`㎜`→`mm`、`℃`→`°C`、`～`→`~`、`²`→`2`、`µ`→`μ`。**正規表現はNFKC後の文字に対して書く**（`～` ではなく `[~～]`、`mm²` ではなく `mm2`）
- 数値より前に文字があるもの(`約85mm`, `φ10`)は C101。`sub` で落とすか `match` で数値部分だけ取り出す
- 数値のあとの残り全部を単位として読む。`4(二重)` は C103。`match` で分ける

## 2. キー表
`[settings]` `encoding_in`(utf-8-sig) `encoding_out`(utf-8-sig) `passthrough`(true) `keep`([]) `reserved`(['^NG\d*$']) `on_error`(keep|empty) `rare_shape`(0.05。C131 の閾値、0 で無効) `null_values`([]。空として扱う値)

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

## 2a. 短く書く（0.9.0）— 新しく書く設定はこの形を使う
- **まず `tenugui schema --format json`** を読む。キー・型・既定値・`requires` / `conflicts`・preset の部品名・報告コードが全部入っている（この文書より新しい）
- 同じルールを複数の列に: `src = ["a", "b"]`。列名に規則があるなら `for = { n = ["0", "1"] }` + `src = "wire{n}_len"`（全ての組み合わせに展開。`out` に `{n}` と `{src}` を書ける）。展開後の出力列名が重複すると設定エラー
- 部品に共通の指定は `each = { number_only = true, strip_units = ["mm"] }`（部品側が優先。case 形式では field の each が全 case に、case の each がその case に効く）
- 出力列名だけの部品は短縮形: `parts = { nom = "len_nom", tol = "len_tol" }`
- `[settings] null_values = ["-", "なし"]`: ルールのかかった列で空と同じに扱う（NFKC・大小無視の完全一致）。ルールのない列と元の列は変えない。try の trace では stage `null`
- 標準形:
  ```toml
  [[field]]
  for = { n = ["0", "1"] }
  src = "wire{n}_length"
  drop_src = true
  preset = "tolerance"
  each = { number_only = true, strip_units = ["mm"] }
  parts = { nom = "wire{n}_len_nom", tol = "wire{n}_len_tol" }
  ```

## 2b. 書式の指定（0.7.0）: `pattern` と `[[field.case]]`
- `pattern = "#±#mm"`（+ `parts`）: 簡易記法。`#`=数値（部品名 n1, n2, …）、`{name}`=名前付き数値、`[...]`=省略可。数値直後の文字はその数値の単位で、**書いた単位にだけ一致**。単位のない数値は同じ括弧区間の隣の単位を借りる。空白・全角・`～` は吸収。`# [ ] { }` を文字として含む値には使えない（`match` を使う）
- 部品は preset と同じ扱い: `to` か `number_only` があれば単位付きで解釈、どちらも無ければ数値の文字列だけ
- `[[field.case]]`: 1つの `[[field]]`（キーは `src`, `drop_src`, `case` のみ）の下に並べる。各 case は `pattern` | `preset` | `match` のどれか1つ + `parts`。上から順に試し最初の一致を使う。同じ `out` を複数 case で使える。一致した case に無い out は空。全て不一致は C111（1行1件、メッセージに形）。case の parts では `sub` 不可
- 形 = NFKC後に数値を `#` にした文字列（`330±15mm` → `#±#mm`、桁区切りは残る: `1,200V` → `#,#V`）。**survey / issue_groups / suggest が返す `shape` は、そのまま `pattern` に書ける**

## 2d. 訂正・保留（0.11.0）— ルールTOMLとは別のファイル
- 場所: ルールTOML `R.toml` と同じフォルダの `R.corrections.toml`（あれば自動で読む。`--corrections PATH` で指定も可）。無ければ今までと同じ動作
- どこに書くかの判断:
  | 値の状態 | 書く場所 |
  |---|---|
  | 意味が同じ言い換え・単位や書式の違い | ルールTOML（`synonyms` / `pattern` / `case`） |
  | 誤記で、正しい値が元データだけから言い切れる | `[[correct]]` |
  | 2通りに読める・設計者に聞かないと決まらない | `[[hold]]`（推測で直さない） |
  | 値そのものの設計上の誤り | tenugui では直さない（後段のチェッカーの仕事） |
- 書き方（`schema_version = 1` 必須）:
  ```toml
  schema_version = 1
  [[correct]]
  column = "outer_dia"          # 入力の列名
  value = "35nm"                # 元の値。完全一致（全角半角・前後の空白の違いは無視）
  to = "35mm"                   # 訂正後。この値を通常のルールにかける
  reason = "外径が nm になることはない"   # 必須
  key = ["P002"]                # 任意: 先頭列（シート名）で絞る。省略 = 列の全行。key 付きが key なしより優先
  ref = "照会先・図面番号"        # 任意
  [[hold]]
  column = "test_volt"
  value = "100/200V"
  reason = "併記か範囲か決められない"     # 必須
  ```
- 設定エラーになるもの: `reason` なし / `value` と `to` が同じ / 同じ（列・値・key）が2つ / 訂正後の値が別の訂正・保留の対象 / 抽出NG印を対象にした / 未知のキー
- `[[correct]]`: 入力のその列の値を置き換えてからルールにかける。ルールのない列にも効く（変換前後チェックは訂正を意図した変更として扱う）。`changes[].kind = "correct"`
- `[[hold]]`: その列のルールをかけない。元の列は元の値のまま、別名の出力列（`out` ≠ `src`）と、その値を使う派生は空。`C141`(W) を1件報告。派生側は二重に報告しない
- 空セルと抽出NG印には当たらない
- 一度も当たらなかった訂正・保留は `provenance.unused[]`（`L301` 訂正 / `L302` 保留）。元データが直ったなら消す。列名の書き間違いは `column_missing: true`。フォルダ一括では、全ファイルで当たらなかったものだけを `unused[]`（トップレベル）に出す
- `[[test]]` と `try` にも効く（try の trace の stage は `correct` / `hold`）

## 2c. `[[test]]`（0.7.0）
- `in = { 列 = "値" }`（必須）, `out = { 出力列 = "期待値" }`, `codes = ["C111"]`（期待する報告コードの集合。省略時は空=問題なしを期待）, `name`（任意）。`out` か `codes` のどちらかは必須
- `check` が毎回実行し、失敗はエラー扱い（終了コード1）。ルールを変えたら必ず `check`

## 3. 単位式
`mm` `mm^2` `mm2` `mm²` `mm*mm` `m/s` `mm/min` `kg*m/s^2` を解釈。次元の指数が一致すれば換算できる（`mm^2`↔`cm2`）。式定義の単位は単独のみ（`W/°C` は不可）

## 3b. 出力ファイル（convert）
既定の出力先 = 入力CSVのあるフォルダの `tenugui_out/`（無ければ作成）。`--out-dir` で変更、`-o` で出力CSV名、`--timestamp` で `_YYYYmmdd_HHMMSS` 付き。
出力は3つ: `<名>_tenugui.csv`（変換結果）/ `<名>_tenugui.html`（人が見るレポート。1ページ・単体で開ける。問題セルに色）/ `<名>_tenugui.json`（機械が読むレポート `tenugui.report/1`: version, time, command, input, config, config_sha, header_sha, rows, counts, files, issue_groups[], case_coverage[], issues[]（全件・raw つき）, changes[{row,key,column,before,after}], verification, not_idempotent[], lint[], header_drift）。**後から結果を調べるときは JSON を読む**。訂正ファイルを使っているときだけ `<名>_tenugui_fix.csv`（元データの修正依頼リスト。列: 種類, row, key, column, 元の値, 訂正後, 理由, 根拠。当たりが無ければ見出しだけ）も書く。JSON レポートと convert の JSON には `provenance:{corrections_file, corrections_sha, counts:{normalize, correct, hold}, fix_requests[{kind, where, column, value, to, reason, ref, count, keys[], rows[], keys_truncated}], cells[{kind, row, key, column, before, after, reason, ref, outputs[]}]（訂正・保留のかかったセル全件。`outputs` はその値から作られる出力列。**後段のツールは kind = hold の行の column と outputs を未検査として扱う**）, unused[{code, kind, where, column, value, to, key[], reason, column_missing, message}], glossary[{set, canonical, total, unified, variants[{value, count, canonical}], columns[]}]}` が入る（訂正ファイルが無くても `counts.normalize` と `glossary` は入る）。`changes[]` の各要素に `kind`（`normalize` | `correct`）。HTML は 表記統一=緑 / 訂正=青 / 保留=紫 で色分けし、修正依頼リストと用語対応表（この入力で使われた表記と件数）の節が付く。`--detail-files` で従来の `_conv.csv`（問題一覧）`_diff.csv`（変更セル。`--no-diff` で省略）`.log` も書く

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
| C131 | W | 変換はできたが、その列では少数派の形（単位なし・桁区切り `1,200`・書き方違い）。値のある行が20以上の、数値として読む通常の field と preset が対象（pattern / match / case は対象外） | 正しければ放置可。別の書式なら case を足す。無効化は `[settings] rare_shape = 0`（既定 0.05 = 5%以下） |
| C141 | W | 保留（訂正ファイルの `[[hold]]`）。変換せず元の値のまま通した | 判断がついたら `[[hold]]` を消し、誤記なら `[[correct]]`、正しい値ならルールを足す。残っていたら止めたいときは `--fail-on hold` |
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
| L301 | W | 当たらなかった訂正 | 元データが直ったなら `[[correct]]` を消す。違うなら column / value の綴りを確認 |
| L302 | W | 当たらなかった保留 | 同上 |
| L303 | W(check) | 訂正・保留の列が入力に無い | column を直す |

## 5. CLI と JSON（`--format json` は stdout に1行のJSON）
`--fail-on` は none | error | hold | warn。`hold` = エラーがあるか、保留（C141）が1つでも残っていれば終了コード1（自動化に流す前の関門に使う）。`-c` を取るコマンドは `--corrections PATH` も取る。
終了コード: 0 OK / 1 `--fail-on` 基準に該当 / 2 設定・使い方のエラー（JSON時も `{"schema":"tenugui.error/1","ok":false,"config_errors":[{where,message}]}` を stdout に出す）
- `tenugui check -c R [IN] [--idempotent] [--format json] [--fail-on error|warn|none(既定error)]` → `{schema:"tenugui.check/1", ok, fields, derives, input_problems[], lint[], duplicate_columns[], header_drift, not_idempotent[]}`
- `tenugui convert IN -c R [-o OUT] [--out-dir D] [--timestamp] [--detail-files [--no-diff]] [--verify] [--max-issues N] [--format json] [--fail-on ...(既定none)]` → OUT.csv / OUT.html / OUT.json（`--detail-files` で OUT_conv.csv(row,key,column,code,severity,message,raw,out), OUT_diff.csv(row,key,column,before,after), OUT.log）。JSON: `{schema:"tenugui.convert/1", ok, rows, counts:{error,warning}, files{output,html,json}, issues[{row,key,column,out,code,severity,message,raw,hint}], issues_truncated, changed_cells, not_idempotent[], lint[], header:{sha,duplicates,drift}}`
- `tenugui try -c R --field 列 値 [--set 列=値 ...]` → CSVなしで1行試す。`{schema:"tenugui.try/1", ok, outputs{}, issues[], trace{出力列:[{stage,value}]}, skipped_fields[], skipped_derives[]}`。stage: null / case / normalize / sub / synonyms / extract / number_only / quantity / format / reserved
- `tenugui survey IN -k 列 [-k 列...] [-c R] --format json` → `{columns:[{column,distinct,top_values[{value,count}],units{},unparsable,candidates[{canonical,variants[],kind:exact|similar,count}],composite_examples[]}]}`
- `tenugui verify IN OUT_CSV -c R [--format json] [--fail-on ...(既定error)]`
- `tenugui draft IN [-c R] [-o draft.toml(既定 tenugui_out/draft.toml)] [--stdout]` → 実態から作る下書き。exact な表記ゆれと、単位付き数値列(80%以上が読める)だけを出し、疑い(similar)は `#` コメントにする。**下書きは必ず `check` と `try` で検証してから使う**
- `tenugui survey IN [--all] --format json`（`-k` なし）→ 全列の概況 `{schema:"tenugui.survey_overview/1", rows, columns:[{column, filled, ng, distinct, shape_count, shapes[{shape,count,distinct,examples[]}]}]}`。既定は数値を含み形が2種類以上の列だけ。`-k 列` の結果にも `shapes[]` が入る
- convert の JSON に追加: `issue_groups[{column,out,code,severity,shape,count,examples[],rows[],message,hint}]`（件数順）, `case_coverage[{column,cases[{case,label,count}],unmatched,empty,ng}]`。**大きなCSVでは `--max-issues 0` を付け、`issue_groups` だけ読む**
- `tenugui suggest IN -c R [--max-groups N] --format json` → `{schema:"tenugui.suggest/1", rows, groups:[{...issue_groupsと同じ..., suggestion:{kind, toml, note, confidence}}], groups_truncated}`。kind: `add_case`（既存の case 形式に足す）/ `to_case`（単一の書式を case 形式に直して足す）/ `to_pattern`（plain な field を分割に変える）/ `use_to` / `add_strip_unit` / `add_default_unit` / `add_synonym` / `none`。confidence: medium / low / none。**部品と出力列の対応は左からの順の推測**（`#~#mm` の2つ目は許容差ではなく上限、など）なので必ず意味を確認する。ファイルは書かない
- `tenugui test -c R --format json` → `{schema:"tenugui.test/1", total, failed, ok, failures:[{name, in, diffs:[{column, expected, actual}]}]}`。終了コード 0/1。check の JSON にも `tests` が入る
- `tenugui try ... --as-test [--name 名前]` → 今の結果を `[[test]]` のTOMLとして出力（貼る前に値が正しいか確認する）
- `tenugui impact IN -c 新R --base 旧R --format json [--fail-on error]` → `{schema:"tenugui.impact/1", same, rows_before, rows_after, columns_added[], columns_removed[], changed_cells, columns:[{column, changed, samples[{row,key,before,after}]}], issues_before{error,warning}, issues_after, issue_changes[{column,out,code,before,after}]}`。`--fail-on error` は違いがあれば終了コード1
- `tenugui compare 旧OUT.csv 新OUT.csv --format json` → 同じ形（issues_* なし）。行は行番号で突き合わせる
- `tenugui --version`（`-V`）→ `tenugui 0.10.0`
- `tenugui schema [--format json]` → `{schema:"tenugui.schema/1", version, schema_version, toml:{settings, units, unit_alias, synonyms, field:{forms:{plain,split,case}, value_keys, part_only_keys, order}, derive, test}, presets{名前:{parts[]}}, preset_examples, codes{コード:{severity, meaning, hint}}, notes[]}`。各キーは `{type, desc, default?, required?, requires?, conflicts?}`
- `tenugui convert フォルダ|IN1 IN2 ... -c R --format json` → `{schema:"tenugui.batch/1", ok, totals:{files, failed, rows, error, warning}, results:[convert と同じ辞書...]}`。フォルダは中の `*.csv`（サブフォルダは見ない）。列が足りないファイルは `results[].input_problems` に入り、残りは続行。終了コードは最も悪いもの。`-o` は使えない
- `tenugui glossary -c R [IN] [--format text|json|csv]` → 用語対応表（同義語表を「正式名 ← 別名」にしたもの。配布用）。`{schema:"tenugui.glossary/1", rows, terms:[{set, canonical, variants[], columns[], count?, unified?, used?[{value,count}]}]}`。IN を渡すと、実際に使われた件数（count = 全体、unified = 正式名と違う表記の件数）が付く
- `tenugui lock IN -c R` → R.lock.json に列構成を記録。以後 check/convert が列の増減を C003 で報告

## 6. 推奨ループ（agent）
0. `schema --format json` で書き方を確認する
1. `survey IN --format json` で、ばらつきのある列と形を把握する（1列を詳しく見るなら `-k 列`）。または `draft`
2. TOML を書く。複合表記は形をそのまま `pattern` に書き `parts` で列に割り当てる。形が複数ある列は `[[field.case]]`。正規表現（`match` / `extract` / `sub`）は TOML の**リテラル文字列 `'...'`**で書く（`"\d"` は構文エラー）
3. 代表的な値を `[[test]]` にする（`try ... --as-test` の出力を、値を確認してから貼る）
4. `check -c R IN --format json --idempotent` → `config_errors` / `input_problems` / `lint` / `tests.failures` を直す
5. `convert IN -c R --verify --format json --max-issues 0` → `issue_groups` を見る
5b. ルールで直せない値は 2d の表で振り分ける: 自明な誤記 → `[[correct]]`、判断が要る → `[[hold]]`。**理由は「なぜそう言えるか」を元データの事実で書く**（人が修正依頼として読む）。迷ったら必ず `[[hold]]`
6. 問題が残れば `suggest IN -c R --format json` → `suggestion.toml` を、`note` を読み意味を確認してから設定に入れる → 4 に戻る
7. 既存の設定を直したときは `impact IN -c 新R --base 旧R --format json` で、意図した列・行だけが変わったか確認する
8. 難しい値は `try --field 列 値 --format json` で `trace` を見る（case 形式は最初の stage が `case`）
9. 本番の列構成が固まったら `lock`
10. 完了条件: `provenance.unused == []`、保留は人が判断するまで残してよい（`provenance.counts.hold` を報告する）、`counts.error == 0`（残すと決めた問題を除く）、`tests.failed == 0`、`verification.ok`、`not_idempotent == []`、`header.drift` が null か changed=false

## 6b. 「A,B,CならA」
- 値の統一（B・C も A）: `[synonyms.g]` に `"A" = ["B", "C"]`、field に `synonyms = "g"`。突合は完全一致（NFKC・大小無視・空白と `-_・` を除去）。部分一致はしない。表に無い値は `on_unknown`（warn/keep/error）
- セル内の並び（`A,B,C` の先頭）: `extract = '^([^,、]+)'`。最後なら `extract = '([^,、]+)$'`

## 6c. 書き方が混ざる列（`examples/mixed_formats.toml` に動く例）
- `7±2mm` / `7mm±2` / `7mm±2mm` の混在 → `preset = "tolerance"` 1つで全部通る（単位は値の中で共有）。plain な `number_only` のままだと `C104 指定外の単位 '±2mm'` になる
- `3400W+238-272(15.64~18.19A)`（± でなく + と -）→ `pattern = "{w}W+{plus}-{minus}({lo}~{hi}A)"`。± の書き方と混ざるなら `[[field.case]]` で `preset = "tolerance_range"` と並べる。全角の ＋ － ～ は半角と同じ扱い
- 数値の列に決まった文字が混ざる（`8mm`, `10mm`, `分光`）→ `[[field.case]]` で、1つ目 `pattern = "#mm"` を数値の列へ、2つ目 `match = '^(?P<kind>分光|拡散)$'` を文字の列へ。決めていない文字は C111 で気づける

## 7. 落とし穴
- TOML の通常文字列で `"\d"` `"\s"` は構文エラー → `'\d'`
- TOML 1.0 はインラインテーブル内の改行不可。`parts = { ... }` は1行で書く（長ければ `[field.parts.x]` サブテーブル形式でも可）
- `[synonyms.x]` を作ったら field から `synonyms = "x"` で参照する（未参照は L201）
- 数字だけ違う値（型番・番号・寸法）は同義語として束ねない（draft/survey も束ねない）
- 実データ由来の列名・値・型式名は共有物（README・examples・テスト）に入れない。**訂正ファイルと同義語表の実物は社内の語彙そのもの**なので、公開リポジトリには置かない
- 表記ゆれを `[[correct]]` に書かない（用語対応表に載らず、修正依頼リストが膨らむ）。逆に、誤記を `synonyms` の別名として書かない（誤記が「正しい別名」として配られてしまう）
