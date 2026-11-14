# Tenugui

chozuの出力CSVを、TOMLルールで変換する。標準ライブラリのみ（Python 3.11+）。
手水(chozu)で手を洗ったあとに拭く → 手拭い(tenugui)。

```
pip install git+https://github.com/KamiyaK/tenugui      # 版を指定するなら末尾に @v0.8.0
tenugui --version
```

```
tenugui survey  in.csv -c rules.toml -k 列名      # ルールを書く前に値の実態を数える
tenugui check   -c rules.toml [in.csv]            # 設定の検証（CSVを渡すと列の存在・ヘッダ変化も確認）
tenugui draft   in.csv -o draft.toml              # 実態からルールの下書きを作る
tenugui try     -c rules.toml --field 列 値        # CSVなしで1つの値を試す
tenugui ui                                        # 簡易UI
tenugui convert in.csv -c rules.toml              # in.csv のあるフォルダの tenugui_out/ に 結果CSV・HTMLレポート・JSONレポート
tenugui verify  in.csv out.csv -c rules.toml      # 変換前後の突き合わせ検証
```

## 処理順（1つの [[field]]）
空/NG印はそのまま通す → NFKC正規化(全角→半角, ㎜→mm) → `sub` 正規表現置換 →
`synonyms` 同義語表 → `extract` 数値部分の抽出 → `to` 単位換算 → `decimals`/`unit_out`

## 主なキー
| 場所 | キー | 意味 |
|---|---|---|
| `[settings]` | `passthrough` `keep` `on_error` `reserved` | 未指定列を通すか / 失敗時に元値を残すか / chozuのNG印の正規表現 |
| `[units.<次元>]` | `base`, 単位 = 倍率 | 既定表(長さ・電圧・電流・抵抗・電力・質量・時間・圧力・周波数)への追加 |
| `[unit_alias]` | `"ミリ" = "mm"` | 単位の表記ゆれ |
| `[synonyms.<名>]` | `正規名 = [ゆれ...]` | 大小・全半角・空白・ハイフンを無視して突合 |
| `[[field]]` | `src` `out` `drop_src` `required` | `out` が違えば元列は残し直後に追加、同じなら置換 |
| | `sub` `synonyms` `on_unknown` `extract` | `on_unknown` = keep / warn / error |
| | `to` `default_unit` `unit_out` `unit_label` `decimals` `trim` | `unit_out` = strip(数値のみ) / add(単位を付ける) |
| `[[derive]]` | `out` `expr` `vars` `unit` `unit_out` `decimals` `after` | 宣言順に評価。`vars = { d = "列名@mm" }` で換算してから計算 |

式で使えるもの: 数値, 変数, `+ - * / **`(整数指数), 括弧, `pi`, `abs min max round sqrt`。

変更履歴は [CHANGELOG.md](CHANGELOG.md)。版は `tenugui --version` で確認できる。

## 0.8.0 の追加

**出力は3つ** — `tenugui_out/` に次を書く。

| ファイル | 用途 |
|---|---|
| `<名>_tenugui.csv` | 変換結果 |
| `<名>_tenugui.html` | 人が見るレポート（1ページ。ブラウザで開くだけ） |
| `<名>_tenugui.json` | 機械が読むレポート（問題の全件・変更セル・前後チェック） |

従来の `_conv.csv`・`_diff.csv`・`.log` は `--detail-files` を付けたときだけ出す（同じ内容は JSON に入っている）。

**HTMLレポート** — ルールのかかった列について「元の値 → 変換後」を並べ、セルに色を付ける。
赤 = 変換できなかった値、黄 = 変換したが要確認、緑 = 変換した値、灰 = 抽出NG印。「問題のある行だけ表示」で絞れる。
2000 行を超える入力では、問題のある行だけを表に載せる。

**少数派の形の警告（C131）** — 変換はできたが、その列の中で少数派の形の値を警告する（`7mm` が並ぶ列の `7` や `1,200mm` など）。
誤変換の見落としを減らすためのもので、値は変換する。値のある行が 20 以上の列が対象。`[settings] rare_shape = 0` で無効、既定は 0.05（5% 以下）。
`pattern` / `match` / `case` で形を明示した列は対象外。

## 0.7.0 の追加（値のばらつきへの対応）

**形の集計** — 数値を `#` に置き換えた「形」ごとに件数を出す。どんなばらつきがあるかを最初に把握する。

```
tenugui survey in.csv              # 数値を含み、形が2種類以上ある列の概況
tenugui survey in.csv -k 列名       # 1列を詳しく（形・値・単位・表記ゆれ候補）
```

**pattern（簡易記法）** — 形をそのままルールに書ける。正規表現は不要。

```toml
[[field]]
src = "wire_length"
pattern = "#±#mm"          # 7±2mm → 7, 2
parts = { n1 = { out = "len_nom", number_only = true, strip_units = ["mm"] }, n2 = { out = "len_tol", number_only = true, strip_units = ["mm"] } }
```

- `#` は数値（左から `n1`, `n2`, …）。`{nom}` と書けば名前を付けられる
- `[ ... ]` は省略可能な部分（`{w}W[±{tol}W][({lo}~{hi}A)]`）
- 数値の直後の文字（`mm`, `W`）はその数値の単位。書いた単位にだけ一致する（`#±#mm` は `7±2cm` に一致しない）
- 単位のない数値は、同じ括弧区間の隣の数値の単位を借りる
- 空白の有無、全角/半角、`～`/`~` の違いは吸収する

**case（複数の書式を順に試す）** — 1つの列に書式が混ざっているとき。上から順に試し、最初に一致したものを使う。

```toml
[[field]]
src = "wire_length"
drop_src = true
[[field.case]]
pattern = "#±#mm"
parts = { n1 = { out = "len_nom", number_only = true, strip_units = ["mm"] }, n2 = { out = "len_tol", number_only = true, strip_units = ["mm"] } }
[[field.case]]
pattern = "#mm"                      # 許容差なし → len_tol は空
parts = { n1 = { out = "len_nom", number_only = true, strip_units = ["mm"] } }
```

- 各 case は `pattern` / `preset` / `match` のどれか + `parts`。同じ出力列名を複数の case で使える
- 一致した case に無い出力列は空。どれにも一致しなければ C111（メッセージに形が入る）
- convert は「書式の内訳」（どの case で何件通ったか・不一致の件数）を表示・ログに出す

**問題の内訳** — convert は問題を「列 × コード × 形」でまとめて表示する（JSON は `issue_groups`）。

**suggest** — まとめた問題ごとに、設定の修正案（TOML断片）を出す。案は推測なので、適用後に `check` で確かめる。

```
tenugui suggest in.csv -c rules.toml
```

**回帰テスト** — 設定の中に「入力 → 期待値」を書いておく。`check` と `test` で実行され、ルールを直したときに壊れていないか分かる。

```toml
[[test]]
name = "許容差つき"
in = { wire_length = "330±15mm" }
out = { len_nom = "330", len_tol = "15" }

[[test]]
in = { wire_length = "不明" }
codes = ["C111"]                     # 期待する報告コード（省略時は「問題なし」を期待）
```

`tenugui try -c rules.toml --field wire_length "330mm" --as-test` で、今の結果を `[[test]]` として出せる。

**影響差分** — ルールを変えたとき、同じ入力の出力がどう変わるかを出す。

```
tenugui impact in.csv -c rules.toml --base rules_old.toml     # 設定の前後
tenugui compare old_out.csv new_out.csv                        # 出力CSV同士
```

## 0.6.0 の追加
- **`number_only` は「指定した単位だけ」処理する**: `strip_units` に捨ててよい単位を必ず書く。それ以外の単位は黙って捨てず `C104` エラーにして元の値を残す。換算はしない
  ```toml
  [[field]]
  src = "len"
  number_only = true
  strip_units = ["mm", "cm"]     # 330mm → 330、12cm → 12（120にはならない）。5V や 20% は C104
  ```
  単位なしの値はそのまま通る。単位の表記ゆれは `[unit_alias]` が効く（`5ボルト` を V として許可できる）。`個` のような単位表に無い語も書ける。何でも捨てたいときだけ `strip_units = ["*"]` と明示する。`to`/`default_unit`/`unit_out`/`unit_label` とは併用不可
- **元のデータを消す `drop_src = true`**: 分割・変換したあと元の列を出力から消す。`preset`/`match` の行にも、`out` を別名にした通常の field にも書ける
  ```toml
  [[field]]
  src = "spec"
  preset = "tolerance_range"
  drop_src = true
  parts = { nom = { out = "w", to = "W" }, rlo = { out = "a_min", to = "A" } }
  ```
  変換に失敗した行の元の値は、出力CSVからは消えるが `*_conv.csv` の raw とログに記録される（ログに「削除した元の列」と失敗行数が出る）。`[[derive]]` は消した列も参照できる。**`drop_src` と、元の列と同じ名前への出力（`out = src`）は同時に使えない**（列が消えるため設定エラー）。前後チェックでは意図した削除は違反にしない

## 0.5.0 の追加
- **`preset = "tolerance_range"`**: `1250W±100W(11.5～13.5A)` → `1250W` / `100W` / `11.5A` / `13.5A`（部品名 nom / tol / hi / lo / rlo / rhi。`to` と `unit_out = "add"` で単位つきのまま、単位が違えば換算して出力）
  ```toml
  [[field]]
  src = "spec"
  preset = "tolerance_range"
  parts = { nom = { out = "w", to = "W", unit_out = "add" }, tol = { out = "w_tol", to = "W", unit_out = "add" }, rlo = { out = "a_min", to = "A", unit_out = "add" }, rhi = { out = "a_max", to = "A", unit_out = "add" } }
  ```
- **「A,B,CならA」の使い方**: `examples/abc.toml` 参照。(1) 値が A でも B でも C でも A にそろえる → `[synonyms.x]` に `"A" = ["B", "C"]` と書き、field から `synonyms = "x"` で参照。(2) セルが `A,B,C` と並んでいるとき先頭の A だけにする → `extract = '^([^,、]+)'`

## 0.4.0 の追加
- **1つの値の分割 `preset`**: `100±5V` → (100, 5) のような典型形は正規表現なしで書ける。
  ```toml
  [[field]]
  src = "volt"
  preset = "tolerance"          # 100±5V / 100V±5V / 100±5 / 100+5/-3V / φ13±0.2 / 100V
  parts = { nom = { out = "v_nom", to = "V" }, tol = { out = "v_tol", to = "V" } }
  ```
  `tolerance` は部品 nom / tol / hi / lo、`range`（`11.5～13.5A`）は lo / hi。`to` を付けた部品は、同じ値の中の単位を共有して換算する（`1.2kV±50V` → 1200, 50）。許容差が無い値（`100V`）は tol が空になる。括弧つきなど複雑な形は `match`/`parts`
- **出力は `tenugui_out` フォルダにまとめる**: 既定は入力CSVのあるフォルダの `tenugui_out/`（無ければ作る）。変換結果 `*_tenugui.csv`、変換レポート `*_conv.csv`、差分 `*_diff.csv`、実行ログ `*.log` が入る。`--out-dir` で場所、`--timestamp` で日時付き、`--no-diff` で差分なし。`draft` の既定出力も同じフォルダ
- **変換前後チェック**: 変換のたびに自動で突き合わせる。行数、ルール対象外の列が変わっていないか、抽出NG印が保たれているか、列が消えていないか、値が空になった(欠落)セルを検査。`tenugui verify 入力.csv 出力.csv -c ルール.toml` は、ディスク上の出力が設定と食い違う（古い・手編集）ことも検出する
- **UI**: 「前後チェック」タブ（列ごとの要約・違反・行を指定した前後表示・出力CSVの読み直し検証）、「出力フォルダを開く」ボタン

## 0.3.0 の追加
- **複合表記の分解**: `match`（名前付きグループ）＋`parts` で1列を複数列へ（`φ13±0.2` → 13 と 0.2、`1250W±100W(11.5～13.5A)` → 4項目、`4(二重)` → 数と注記）。`unit_group` で単位を共有、省略可能な部分は空になる
- `convert --diff`（値が変わったセルの前→後を `*_diff.csv`）／ `--verify`（結果を再度通して値が変わる列＝冪等でない列を検出）／ `check` の lint（L101: 単位なし出力で default_unit なし 等）
- `lock`: 列構成を `*.lock.json` に記録し、以後 chozu 側の列の増減を C003 で警告
- `survey` が表記ゆれ候補（exact / similar）と複合表記の例を出す。`draft` が実態からルールの下書きを作る（数字違いの型番は束ねない）
- agent向け: `--format json`（check/convert/survey/try）、`try`（CSVなしで段階ごとの途中経過を見る）、`--fail-on`、問題ごとの `hint`、`AGENT_REFERENCE.md`
- 簡易UI: `tenugui ui`（tkinter。検証・変換・問題一覧・変更・プレビュー・試し打ち）

## 方針（決めたこと）
- 換算できるのは単位表にある同一次元だけ。未定義の単位・次元違い・数値で読めない値は **黙って通さず** 変換レポートに出す
- 失敗時は既定で元の値を残す（`on_error = "empty"` で空にもできる）
- 派生値は、入力が空なら空、NG印なら空＋警告、変換失敗なら上流で報告済みとして二重報告しない
- 重複する列名は `name#2` のように一意化して出力する

## レポートのコード
C001/C002 設定と入力列の不一致 / C101 数値+単位として読めない / C102 単位なし・default_unit未指定 /
C103 換算不可 / C111 extract不一致 / C121 同義語表に該当なし / C201 必須が空 /
C301 派生: 変数が読めない / C302 派生: NG印入力(W) / C303 計算不可 / C304 単位省略を仮定(W) /
C305 派生: 換算不可 / C311 単位付き生値に `@単位` 指定なし

## 複合単位と式定義の単位（0.2.0）
- `mm^2` `mm2` `mm²` `mm*mm` `m/s` `kg*m/s^2` を単位として解釈し、同じ次元同士なら換算する（`100mm^2` → `to = "m2"` など）
- 倍率で換算できない単位は式で定義する（変数は x）。℃・°F は既定で入っている:
  `[units.temperature]` に `"°C" = { to_base = "x + 273.15", from_base = "x - 273.15" }`
- 式定義の単位は複合単位の中では使えない（`W/°C` はエラー）
- `[[derive]]` は `unit`（式の結果の単位）と `to`（その単位へ換算して出力）を持てる
- 別名列(`out` が `src` と違う)で変換に失敗したときは空にする（生の複合文字列を複製しない）

`examples/` に sample.toml と sample_input.csv。テストは `pytest`。
