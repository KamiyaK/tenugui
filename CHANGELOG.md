# 変更履歴

書き方は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に、版番号は [セマンティック バージョニング](https://semver.org/lang/ja/) に沿う。
1.0.0 までは、設定ファイル（`schema_version = 1`）の既存の書き方の意味は変えない方針。

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

[0.8.0]: https://github.com/KamiyaK/tenugui/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/KamiyaK/tenugui/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/KamiyaK/tenugui/releases/tag/v0.6.1
