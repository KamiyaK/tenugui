# Tenugui AGENT_REFERENCE（agent向け・機械向けの仕様）

動機や比喩は書かない。人向けは README.md。バージョン 0.18.0 / schema_version = 1。

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
- 数値に溶け込ませない文字: 丸数字 `①` は `(1)`、数字の直後の上付き・下付き数字 `5²` は `5^2` にしてから読む。`5①` `5²` は 51・52 ではなく、読めない値として報告する（C101 / C103 / C104 / C111）。単位の後ろ（`mm²`）は今までどおり `mm2`
- 桁区切り: `1,200` `12,345,678` `1,234.5` だけを数値として読む（先頭 1〜3 桁で 0 始まりでない、あとは 3 桁ずつ）。`0,500` `12345,678` `3,4` は C101
- マイナス: `−`(U+2212) `‐` `–` `－` は `-`。長音 `ー` は値の先頭で数字の直前にあるときだけ `-`（単位の別名 `メートル` `オーム` の中はそのまま）
- 数値より前に文字があるもの(`約85mm`, `φ10`)は C101。`sub` で落とすか `match` で数値部分だけ取り出す
- 数値のあとの残り全部を単位として読む。`4(二重)` は C103。`match` で分ける

## 2. キー表
`[settings]` `encoding_in`(utf-8-sig) `encoding_out`(utf-8-sig) `passthrough`(true) `keep`([]) `reserved`(['^NG\d*$']) `on_error`(keep|empty) `rare_shape`(0.05。C131 の閾値、0 で無効) `null_values`([]。空として扱う値) `ignore_columns`(['missing_info\d*(#\d+)?']。draft・survey の対象外の列。列名に全体一致。書くと既定を置き換える)

`[units.<次元>]` `base`=基準単位、`単位 = 倍率`（文字列可: `inch = "25.4"`）。式定義: `"°C" = { to_base = "x + 273.15", from_base = "x - 273.15" }`（変数は x のみ、base は倍率で定義された単位）
既定の次元: length mm / voltage V / current A / resistance Ω / power W / mass g / time s / pressure Pa / frequency Hz / ratio % / temperature K(°C, °F は式)
`[unit_alias]` `"ミリ" = "mm"`
`[synonyms.<名>]` `正規名 = [ゆれ...]`（突合は NFKC・大小無視・空白と `-_・` を除去）

`[[field]]`（通常）`number_only`+`strip_units`(number_only=true のとき strip_units=[捨ててよい単位...] 必須。先頭の数値だけ取り、strip_units にある単位は換算せず捨てる。単位なしの値は通す。それ以外の単位は C104。`["*"]` は何でも捨てる。`to` `default_unit` `unit_out` `unit_label` とは併用不可) `src` `out`(=src: 置換 / ≠src: 元列の直後に追加) `drop_src` `required` `sub`=[[regex,repl],...] `synonyms` `on_unknown`(warn|keep|error) `extract`(group 1 を採用) `to` `default_unit` `unit_out`(strip|add) `unit_label` `decimals` `trim`
`[[field]]`（preset分解）`src` `preset`(tolerance|range) `parts`={ 部品名 = { out, to, default_unit, ...通常キー } } `drop_src`
- tolerance: 部品 `nom` `tol` `hi` `lo`。形: `100±5V` `100V±5V` `100V±5` `100+5/-3V` `φ13±0.2` `100V`（許容差なし→tol は空）。先頭の `φΦØ` は無視
- range: 部品 `lo` `hi`。形: `11.5~13.5A` `5A~10A` `500mA~1.2A`（`～`はNFKCで`~`になる。`-`区切りは負数と区別できないため非対応）
- `to` を付けた部品は、同じ値の中の単位を共有する（nom は先頭側の単位、tol/hi/lo と range の hi は末尾側の単位を優先）。`to` なしの部品は数値テキストのみ（単位は捨てる）。ただし **`to` なしの部品で、値の中の2か所の単位が違うとき（`100V±5%`、`100kV±5V`、`500mA~1.2A`）は C103**（数値だけを取ると意味が変わるため。`to` を付けるか `[[field.case]]` で分ける）
- preset の数値は桁区切りを読む（`1,200±50W` → 1200 / 50）。単位に `, ; 、` は含めない。数字のあとに文字が続く並び（`100V-5V`、`100mm,200mm`）は別の値なので不一致（C111）。`mm2` `min-1` のように数字で終わるものは単位
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
- 符号: `#` は符号つきの数値に一致する。pattern の先頭・`~` の後・`(` の後に書いた `-` は数値の符号（`-#mm` は `-5mm` から -5 を取る）。それ以外の `+` `-`（`#+#-#W`）は区切りで、後ろの数値は符号なしの大きさ
- 部品は preset と同じ扱い: `to` か `number_only` があれば単位付きで解釈、どちらも無ければ数値の文字列だけ
- `[[field.case]]`: 1つの `[[field]]`（キーは `src`, `drop_src`, `case` のみ）の下に並べる。各 case は `pattern` | `preset` | `match` のどれか1つ + `parts`。上から順に試し最初の一致を使う。同じ `out` を複数 case で使える。一致した case に無い out は空。全て不一致は C111（1行1件、メッセージに形）。case の parts では `sub` 不可
- `warn`（0.18.0、case だけ）: その case に一致したら、値は通常どおり取り出したうえで報告を1件（値1つにつき1件）出す。`warn = "メッセージ"` → `C151`(W)、`warn = "CU001"` → `[codes]` に定義したコード・重大度・意味で報告。報告は値を変えない（`on_error` も効かない）。後段は **warn の値を検査対象のまま扱ってよい**（hold と逆）
  - 付けてよいのは「意味が1通りに決まる書き方違い」だけ（単位の省略、旧書式など）。2通りに読めるなら `[[hold]]`、誤記なら `[[correct]]`。何でも受ける case（`match = '^(?P<t>.+)$'` など）に付けると L601
  - 本来の形の case を先に書く（上から順に試すため）。`case_coverage[].cases[].warn` にその case のコード、`issue_groups` に件数・例・行が出る（元データの書式の修正依頼にそのまま使う）
- `[codes]`（0.18.0）: 利用者が決める報告コード。キーは `CU000`〜`CU999`（CU + 数字3桁。組み込みの C / L は変えられない）。`CU001 = { severity = "W", meaning = "単位なし（記載ルール違反）", hint = "元データの修正を依頼" }`。`severity` は W（既定）| E、`meaning` 必須（報告のメッセージ）、`hint` 任意（JSON の hint）。参照するのは case の `warn` だけ。`[[test]] codes` にも書ける
  ```toml
  [codes]
  CU001 = { meaning = "単位なし（記載ルール違反）", hint = "元データの修正を依頼" }

  [[field]]
  src = "len"
  [[field.case]]                 # 本来の形
  pattern = "#±#mm"
  parts = { n1 = "len_nom", n2 = "len_tol" }
  [[field.case]]                 # 公差なし: 値は取る + C151
  pattern = "#mm"
  parts = { n1 = "len_nom" }
  warn = "公差なし"
  [[field.case]]                 # 単位なし: 値は取る + CU001
  pattern = "#"
  parts = { n1 = "len_nom" }
  warn = "CU001"
  ```
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
出力先の保護（終了コード 2、何も書かない）: 出力CSVと同名で書くレポート類が、入力CSV・ルールTOML・訂正ファイル・lock のどれかに重なる / `-o` の拡張子が `.html` `.json` `.log`。`draft -o` と `survey -o` も、入力・設定に重なる名前は使えない。
書き出しは全部そろってから行う: 途中で失敗したとき（`encoding_out` で書けない文字など）は、何も書かず、前回の出力もそのまま残る。
出力は3つ: `<名>_tenugui.csv`（変換結果）/ `<名>_tenugui.html`（人が見るレポート。1ページ・単体で開ける。問題セルに色。変換前後の表はデータを埋め込み、見えている行・列だけを JS で描く。中身を機械で読むなら JSON を読む。HTML から取り出すなら `tenugui.report.embedded_table(html)`。表のデータは既定で gzip + base64 で埋め込む。`--plain-html` で圧縮しない）/ `<名>_tenugui.json`（機械が読むレポート `tenugui.report/1`: version, time, command, input, config, config_sha, header_sha, rows, counts, files, output_sha256（出力CSVの SHA-256。後段が CSV の書き換えに気づくため）, issue_groups[], case_coverage[], issues[]（全件・raw つき）, changes[{row,key,column,before,after}], verification, not_idempotent[], lint[], header_drift）。**後から結果を調べるときは JSON を読む**。訂正ファイルを使っているときだけ `<名>_tenugui_fix.csv`（元データの修正依頼リスト。列: 種類, row, key, column, 元の値, 訂正後, 理由, 根拠。当たりが無ければ見出しだけ）も書く。JSON レポートと convert の JSON には `provenance:{corrections_file, corrections_sha, counts:{normalize, correct, hold}, fix_requests[{kind, where, column, value, to, reason, ref, count, keys[], rows[], keys_truncated}], cells[{kind, row, key, column, before, after, reason, ref, outputs[]}]（訂正・保留のかかったセル全件。`outputs` はその値から作られる出力列。**後段のツールは kind = hold の行の column と outputs を未検査として扱う**）, unused[{code, kind, where, column, value, to, key[], reason, column_missing, message}], glossary[{set, canonical, total, unified, variants[{value, count, canonical}], columns[]}]}` が入る（訂正ファイルが無くても `counts.normalize` と `glossary` は入る）。`changes[]` の各要素に `kind`（`normalize` | `correct`）。HTML は 表記統一=緑 / 訂正=青 / 保留=紫 で色分けし、修正依頼リストと用語対応表（この入力で使われた表記と件数）の節が付く。`--detail-files` で従来の `_conv.csv`（問題一覧）`_diff.csv`（変更セル。`--no-diff` で省略）`.log` も書く

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
| C103 | E | 未定義の単位 / 次元違い / preset の `to` なしの部品で単位が混ざっている（`100V±5%`） | [units.*]・[unit_alias] に追加、または to を直す。混在は部品に to を付けるか case で分ける |
| C104 | E | number_only で strip_units に無い単位 | strip_units に加える、または to で換算する |
| C111 | E | match/extract 不一致 | `try` で正規表現を試す（NFKC後の文字で書く） |
| C121 | W/E | 同義語表に無い | [synonyms.*] に追加（on_unknown=error のとき E） |
| C131 | W | 変換はできたが、その列では少数派の形（単位なし・桁区切り `1,200`・書き方違い）。値のある行が20以上の、数値として読む通常の field と preset が対象（pattern / match / case は対象外） | 正しければ放置可。別の書式なら case を足す。無効化は `[settings] rare_shape = 0`（既定 0.05 = 5%以下） |
| C151 | W | case の `warn = "メッセージ"` に一致した（書き方が本来と違うが、値は取り出した） | 元データの書式を直してもらう。値の検査は止めない。止めたいときは `--fail-on warn` |
| CU000〜CU999 | W/E | case の `warn = "CU###"` に一致した。意味・重大度は `[codes]` の定義 | `[codes]` の hint に従う。E にすると `--fail-on error` で止まる（値は取り出す） |
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
| L501 | W(lint) | `ignore_columns` に当たる列に field がある（変換はする） | 設計値なら ignore_columns から外す。補助列なら field を消す |
| L401 | W(lint) | `match` / `extract` の正規表現に繰り返しの入れ子（`(a+)+`、`(\d*)*`）。一致しない値で極端に遅くなる | 内側か外側の繰り返しを外す（`(\d+)+` → `\d+`） |
| L601 | W(lint) | warn 付きの case がほぼ何でも受ける。C111（エラー）で止まるべき値まで警告に下がる | 数字・単位などの形を書く。2通りに読めるなら `[[hold]]` |
| C003 | W | lock と列構成が違う（増/減） | 意図した変更なら `tenugui lock` で再記録 |
| L301 | W | 当たらなかった訂正 | 元データが直ったなら `[[correct]]` を消す。違うなら column / value の綴りを確認 |
| L302 | W | 当たらなかった保留 | 同上 |
| L303 | W(check) | 訂正・保留の列が入力に無い | column を直す |

## 5. CLI と JSON（`--format json` は stdout に1行のJSON）
`--fail-on` は none | error | hold | warn。`hold` = エラーがあるか、保留（C141）が1つでも残っていれば終了コード1（自動化に流す前の関門に使う）。`-c` を取るコマンドは `--corrections PATH` も取る。
終了コード: 0 OK / 1 `--fail-on` 基準に該当 / 2 設定・使い方のエラー（JSON時も `{"schema":"tenugui.error/1","ok":false,"config_errors":[{where,message}]}` を stdout に出す）
- `tenugui check -c R [IN] [--idempotent] [--format json] [--fail-on error|warn|none(既定error)]` → `{schema:"tenugui.check/1", ok, fields, derives, input_problems[], lint[], duplicate_columns[], header_drift, not_idempotent[]}`
- `tenugui convert IN -c R [-o OUT] [--out-dir D] [--timestamp] [--detail-files [--no-diff]] [--plain-html] [--verify] [--max-issues N] [--format json] [--fail-on ...(既定none)]` → OUT.csv / OUT.html / OUT.json（`--detail-files` で OUT_conv.csv(row,key,column,code,severity,message,raw,out), OUT_diff.csv(row,key,column,before,after), OUT.log）。JSON: `{schema:"tenugui.convert/1", ok, rows, counts:{error,warning}, files{output,html,json}, issues[{row,key,column,out,code,severity,message,raw,hint}], issues_truncated, changed_cells, not_idempotent[], lint[], header:{sha,duplicates,drift}}`
- `tenugui try -c R --field 列 値 [--set 列=値 ...]` → CSVなしで1行試す。`{schema:"tenugui.try/1", ok, outputs{}, issues[], trace{出力列:[{stage,value}]}, skipped_fields[], skipped_derives[]}`。stage: null / case / normalize / sub / synonyms / extract / number_only / quantity / format / reserved
- `tenugui survey IN -k 列 [-k 列...] [-c R] --format json` → `{columns:[{column,distinct,top_values[{value,count}],units{},unparsable,candidates[{canonical,variants[],kind:exact|similar,count}],composite_examples[]}]}`
- `tenugui verify IN OUT_CSV -c R [--format json] [--fail-on ...(既定error)]`
- `tenugui draft IN [-c R] [-o draft.toml(既定 tenugui_out/draft.toml)] [--stdout]` → 実態から作る下書き。列ごとに 数量（値の5割以上が数値＋既知の単位。最頻の単位に `to`）→ 複合表記（2つ以上の数値・±×～~ を含む値が2割以上。preset tolerance/tolerance_range/range か、形ごとの `[[field.case]]`）→ 表記ゆれ（`[synonyms.<列名>]`。exact と similar の両方。英数字だけ違う値 SUS304/SUS304L は束ねない。値が50種類以下なら全値を表に載せ `on_unknown="warn"`、多ければ `"keep"`）の順に1つ作る。作った下書きは今のデータで変換してみて `# 試行:` にエラー・警告の件数を書く。冒頭の `#   列: ルール|要確認|ルールなし — 理由` が全列の一覧（空と抽出NG印だけの列を除く）。`要確認` は similar を束ねた・数値の出力列名を位置で付けた（`<列>_n1`、数値の個数が違う形は `<列>_<k>値_n1`。単位で区別できれば `<列>_<単位>`）・そろえられない単位がある・試行で問題が出た のどれか。chozu の結合出力（先頭が 元ファイル, シート名）は2列ともキーとして扱う。`[settings] ignore_columns` に当たる列（既定は chozu の補助列 `missing_info\d*`、重複で `#2` が付いたものも）は対象外。**下書きは必ず `check` と `try` で検証してから使う**
- `tenugui survey IN [--all] --format json`（`-k` なし）→ 全列の概況 `{schema:"tenugui.survey_overview/1", rows, columns:[{column, filled, ng, distinct, shape_count, shapes[{shape,count,distinct,examples[]}]}]}`。既定は数値を含み形が2種類以上の列だけ。`-k 列` の結果にも `shapes[]` が入る
- `tenugui survey IN [--all] [-c R] -o FILE` → 概況を表（列, 形の種類, 件数, 形, 例。列名・種類数は各列の先頭行だけ）で書き出す。拡張子 `.xlsx` なら Excel、それ以外は CSV（utf-8-sig）。`--format json` で `{schema:"tenugui.export/1", files[]}`。`-k` と併用は終了コード 2。agent が形を読むなら `--format json`（`-o` なし）のほうが扱いやすい
- convert の JSON に追加: `issue_groups[{column,out,code,severity,shape,count,examples[],rows[],message,hint}]`（件数順）, `case_coverage[{column,cases[{case,label,count}],unmatched,empty,ng}]`。**大きなCSVでは `--max-issues 0` を付け、`issue_groups` だけ読む**
- `tenugui suggest IN -c R [--max-groups N] --format json` → `{schema:"tenugui.suggest/1", rows, groups:[{...issue_groupsと同じ..., suggestion:{kind, toml, note, confidence}}], groups_truncated}`。kind: `add_case`（既存の case 形式に足す）/ `to_case`（単一の書式を case 形式に直して足す）/ `to_pattern`（plain な field を分割に変える）/ `use_to` / `add_strip_unit` / `add_default_unit` / `add_synonym` / `none`。confidence: medium / low / none。**部品と出力列の対応は左からの順の推測**（`#~#mm` の2つ目は許容差ではなく上限、など）なので必ず意味を確認する。ファイルは書かない
- `tenugui test -c R --format json` → `{schema:"tenugui.test/1", total, failed, ok, failures:[{name, in, diffs:[{column, expected, actual}]}]}`。終了コード 0/1。check の JSON にも `tests` が入る
- `tenugui try ... --as-test [--name 名前]` → 今の結果を `[[test]]` のTOMLとして出力（貼る前に値が正しいか確認する）
- `tenugui impact IN -c 新R --base 旧R --format json [--fail-on error]` → `{schema:"tenugui.impact/1", same, rows_before, rows_after, columns_added[], columns_removed[], changed_cells, columns:[{column, changed, samples[{row,key,before,after}]}], issues_before{error,warning}, issues_after, issue_changes[{column,out,code,before,after}]}`。`--fail-on error` は違いがあれば終了コード1
- `tenugui compare 旧OUT.csv 新OUT.csv --format json` → 同じ形（issues_* なし）。行は行番号で突き合わせる
- `tenugui --version`（`-V`）→ `tenugui 0.10.0`
- `tenugui schema [--format json]` → `{schema:"tenugui.schema/1", version, schema_version, toml:{settings, units, unit_alias, synonyms, field:{forms:{plain,split,case}, value_keys, part_only_keys, order}, derive, test}, presets{名前:{parts[]}}, preset_examples, codes{コード:{severity, meaning, hint}}, notes[]}`。各キーは `{type, desc, default?, required?, requires?, conflicts?}`
- `tenugui convert フォルダ|IN1 IN2 ... -c R --format json` → `{schema:"tenugui.batch/1", ok, totals:{files, failed, rows, error, warning}, results:[convert と同じ辞書...]}`。フォルダは中の `*.csv`（サブフォルダは見ない）。列が足りないファイルは `results[].input_problems` に入り、残りは続行。終了コードは最も悪いもの。`-o` は使えない。同じ出力名になる入力（別フォルダの同名 + `--out-dir`）は、2つ目から `<名>_tenugui_2.csv` … に書く（`results[].files` が実際の名前）。フォルダの中にある前回の出力（`<名>_tenugui.csv` と `_fix` `_conv` `_diff`。隣に同名の JSON レポートがあるもの）は入力にせず、`skipped_outputs[]` に入れる。名前で指定したファイルは必ず入力にする
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
- **入力CSVの読み方**: ヘッダより前の `# ` で始まる行だけがコメント（chozu の NG 一覧の1行目）。ヘッダより後ろは `#004` のような値もデータ。改行だけの行は飛ばすが `,,,` は残す。ヘッダより列の多い行に値があれば止まる（終了コード 2）。重複した列名には `#2`, `#3` が付く（chozu の `missing_info0` / `missing_info1` は節ごとに同名で出るので、2つ目以降は `missing_info0#2` …）
- **chozu の `--merge` 出力**は先頭列が `元ファイル`、2列目が `シート名`。tenugui の報告の `key` と訂正ファイルの `key` は先頭列なので、ファイル名になる
- **値 `NG` / `NG1` / `NG2` は抽出NG印として触らずに通す**（`[settings] reserved`）。帳票に本当に「NG」と書いてある値も同じ扱いになる
- **数値の上限**: 10 の ±40 乗、有効数字 28 桁、値の長さ 2000 文字。超えるものは C101（読めない値として元のまま残す）。`1E1250` のような読み違いを通さないため
- **設定ファイルの文字コード**: ルールTOML・訂正ファイルは UTF-8（BOM 付きも可）。Shift_JIS・UTF-16 は設定エラー。`encoding_in` / `encoding_out` に無い文字コード名を書くと設定エラー（check で分かる）
- **lock が壊れている**（手で編集した、途中で切れた）と check / convert は終了コード 2。`tenugui lock IN -c R` で作り直す
- **設定エラーになる書き方（0.13.1）**: `sub` の置換後に無いグループ（`'\1'`）/ `reserved` に空文字へ一致する正規表現（`''`、`'.*'`、`'x?'`。全部の値が NG 印扱いになる）/ 派生の `after` に自分の列名 / `[[correct]]` の `to` が抽出NG印の形
- **`number_only` + `strip_units = ["*"]`** は数値の後ろを何でも捨てる: `10-20` → 10、`1/2` → 1、`100±5` → 100、`12:30` → 12。範囲・分数・時刻が混ざる列には使わない（`["mm"]` のように単位を並べると、それ以外は C104 になる）
- **preset = "range" は大小を見ない**: `10~5A` は lo=10, hi=5 のまま出る。逆転の検出は後段のチェッカーで行う
- **同義語の突合は `-` を無視する**: 同義語表に `"5"` があると `-5` も `5` になる。数値の列に synonyms を使わない
- **pattern**: 数値を隣り合わせて書けない（`##`、`{a}{b}` は設定エラー）。数値の間に空白を書いたら（`# #`）、値にも空白が必要。pattern は 300 文字まで
- **式**: 関数は `abs(x)` `sqrt(x)` `round(x[, 桁])` `min(a, …)` `max(a, …)`。引数の数が違う・関数名を変数のように書く・2000 文字を超える、は設定エラー。0 除算・負の平方根・桁あふれ（結果が有限でない）は C303
- **値の型**: `to = "mm"`（文字列）、`decimals = 2`（整数）、`number_only = true`（引用符なし）、`strip_units = ["mm"]`（配列）、`sub = [['正規表現', '置換後']]`。型が違うと、キー名と期待する形を示す設定エラーになる
- TOML の通常文字列で `"\d"` `"\s"` は構文エラー → `'\d'`
- TOML 1.0 はインラインテーブル内の改行不可。`parts = { ... }` は1行で書く（長ければ `[field.parts.x]` サブテーブル形式でも可）
- `[synonyms.x]` を作ったら field から `synonyms = "x"` で参照する（未参照は L201）
- 数字だけ違う値（型番・番号・寸法）は同義語として束ねない（draft/survey も束ねない）
- 実データ由来の列名・値・型式名は共有物（README・examples・テスト）に入れない。**訂正ファイルと同義語表の実物は社内の語彙そのもの**なので、公開リポジトリには置かない
- 表記ゆれを `[[correct]]` に書かない（用語対応表に載らず、修正依頼リストが膨らむ）。逆に、誤記を `synonyms` の別名として書かない（誤記が「正しい別名」として配られてしまう）
