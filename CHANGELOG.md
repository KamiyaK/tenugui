# 変更履歴

書き方は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に、版番号は [セマンティック バージョニング](https://semver.org/lang/ja/) に沿う。
1.0.0 までは、設定ファイル（`schema_version = 1`）の既存の書き方の意味は変えない方針。

## [0.11.0] - 2026-11-15

元データの問題を「黙って直さない」ための版。訂正ファイルを置かなければ、今までの設定・出力（CSV・終了コード）は変わらない。

### 追加
- 訂正ファイル `<ルール名>.corrections.toml`（ルールTOMLと同じ場所にあれば自動で読む。`--corrections` で指定も可）
  - `[[correct]]` 訂正: 正解が自明な誤記。値を置き換えてからルールにかける。`reason` 必須
  - `[[hold]]` 保留: 判断が要る値。変換せず元の値のまま通し、`C141`（警告）で報告する。`reason` 必須
  - `key` で先頭列（シート名など）に絞れる。`ref` に根拠・照会先を書ける
- 修正依頼リスト `<名>_tenugui_fix.csv`: 訂正・保留のかかったセルを、元の値・訂正後・理由つきで1行ずつ（訂正ファイルを使っているときだけ書く）
- 当たらなくなった訂正・保留の報告（`L301` / `L302`）: 元データが直ったことが分かる
- HTML レポート: 表記統一（緑）・訂正（青）・保留（紫）の色分け、「元データの修正依頼リスト」と「用語対応表（この入力で使われた表記）」の節
- JSON レポート / convert の JSON: `provenance`（件数、修正依頼リスト、当たらなかった訂正・保留、使われた表記）、`changes[].kind`
- `--fail-on hold`: エラーか、保留の値が残っていれば終了コード 1
- `tenugui glossary -c R [IN]`: 用語対応表（正式名 ← 別名）を text / JSON / CSV で出す。CSV を渡すと使われた件数つき
- `tenugui schema` に訂正ファイルの書き方と、どこに書くかの判断基準
- `tenugui try` の途中経過に `correct` / `hold`、`tenugui check` に訂正ファイルの件数と `L303`（列が入力に無い）
- `examples/corrections_demo.toml` と `examples/corrections_demo.corrections.toml`

## [0.10.0] - 2026-11-14

### 追加
- UI: 「形」タブ（形の集計。ルールTOMLが無くても使える）、「内訳と修正案」タブ（問題を種類ごとにまとめ、選ぶと直し方の案を表示）、「レポートを開く」ボタン
- UI: 「検証」が `[[test]]` も実行する
- `examples/mixed_formats.toml`: 1つの列に書き方が混ざる場合の例（許容差の書き方違い、`+`/`-` の許容差、数値に文字が混ざる列）

### 変更
- GitHub Actions の actions/checkout を v5、actions/setup-python を v6 に更新（Node.js 20 の廃止警告への対応）

## [0.9.0] - 2026-11-14

### 追加
- 列のまとめ指定: `src = ["a", "b"]`、`for = { n = ["0", "1"] }` と `src = "wire{n}_len"`（`out` に `{n}` と `{src}`）
- `each`: `parts` の全部品に共通の指定
- `parts` の短縮形: `parts = { nom = "出力列名" }`
- `[settings] null_values`: 空として扱う値
- `tenugui schema`: 設定の書き方（キー・型・既定値・制約・preset・報告コード）を text / JSON で出す
- `convert` にフォルダや複数のCSVを渡せる（JSON は `tenugui.batch/1`）
- エラーメッセージに綴りの候補（キー・列名・単位・preset・同義語表）と、TOML 構文エラーのよくある原因

### 修正
- 入力ファイルが無い・文字コードが合わないときに、トレースバックではなくメッセージを出して終了コード 2 で終わる

## [0.8.0] - 2026-11-14

### 追加
- HTMLレポート `<名>_tenugui.html`（1ページ・単体で開ける）。元の値と変換後を並べ、問題のあるセルに色を付ける
- JSONレポート `<名>_tenugui.json`（`tenugui.report/1`）。問題の全件・変更セル・変換前後チェックを持つ
- 警告 C131: 変換はできたが、列の中で少数派の形の値（`[settings] rare_shape`、既定 0.05、0 で無効）
- `tenugui --version` / `-V`
- GitHub Actions によるテストの自動実行と、main に入った版のタグ・リリースの自動作成

### 変更
- **convert の既定の出力を3つに整理**（変換結果CSV・HTML・JSON）。`_conv.csv`・`_diff.csv`・`.log` は `--detail-files` を付けたときだけ出す
- convert の JSON 出力の `files` は `output` / `html` / `json`（`--detail-files` で `report` / `diff` / `log` が加わる）
- 形の集計で、桁区切りのカンマを残す（`1,200V` は `#,#V`）

## [0.7.0] - 2026-11-14

### 追加
- `survey`: 数値を `#` にした「形」ごとの件数。列を指定しないと全列の概況
- `pattern = "#±#mm"` の簡易記法（`#`、`{名前}`、`[省略可]`）
- `[[field.case]]`: 複数の書式を上から順に試す。書式の内訳を表示
- convert が問題を「列 × コード × 形」でまとめる（JSON は `issue_groups`、`case_coverage`）
- `suggest`: 問題ごとの修正案（TOML断片）
- `[[test]]` と `tenugui test`、`try --as-test`。`check` もテストを実行する
- `impact`（設定の前後での出力の違い）と `compare`（出力CSV同士の比較）

## [0.6.1] - 2026-11-14

### 修正
- preset の部品で `number_only` を使うと `strip_units` の単位チェックが効かなかった

## [0.6.0]

### 追加
- `drop_src = true`: 分割・変換のあと元の列を出力から消す
- 報告コード C104（`strip_units` に無い単位）

### 変更
- **`number_only` は `strip_units` が必須**になり、指定した単位だけを取る（それ以外は C104）

## [0.5.0]

### 追加
- `preset = "tolerance_range"`（`1250W±100W(11.5～13.5A)` を4項目に分ける）
- `number_only`（数値だけを取り出す）

## [0.4.0]

### 追加
- `preset = "tolerance"` / `"range"`: 正規表現なしで値を分割
- 出力を `tenugui_out` フォルダにまとめる。実行ログ
- 変換前後チェック（convert で毎回実行、`verify` コマンド）
- UI に「前後チェック」タブ

## [0.3.0]

### 追加
- `match` + `parts` による複合表記の分解
- `--verify`（再実行しても値が変わらないかの確認）、lint、`lock`（列構成の記録）
- `survey` の表記ゆれ候補、`draft`（ルールの下書き）
- agent 向け: `--format json`、`try`、`--fail-on`、`AGENT_REFERENCE.md`
- 簡易UI（`tenugui ui`）

## [0.2.0]

### 追加
- 複合単位（`mm^2`、`mm*mm`、`m/s`）と、式で定義する単位（℃ など）
- `[[derive]]` の `unit` / `to`

## [0.1.0]

### 追加
- TOML ルールによる chozu 出力CSVの変換: 表記ゆれの統一、単位の換算・除去・付加、派生計算

[0.10.0]: https://github.com/KamiyaK/tenugui/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/KamiyaK/tenugui/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/KamiyaK/tenugui/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/KamiyaK/tenugui/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/KamiyaK/tenugui/releases/tag/v0.6.1
