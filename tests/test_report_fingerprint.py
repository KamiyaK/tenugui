"""レポートに出力CSVの指紋（output_sha256）を残す。後段（design_erc）は、CSV がレポートの後に
書き換えられていないか（手で編集した・別の実行で上書きした）をこれで確かめる。"""
import hashlib
import json

from tenugui.cli import main


def test_report_records_output_sha256(tmp_path, capsys):
    (tmp_path / "in.csv").write_text("k,v\nA,5cm\nB,7mm\n", encoding="utf-8")
    (tmp_path / "r.toml").write_text('schema_version = 1\n[[field]]\nsrc = "v"\nto = "mm"\n', encoding="utf-8")
    assert main(["convert", str(tmp_path / "in.csv"), "-c", str(tmp_path / "r.toml"), "--format", "json"]) == 0
    files = json.loads(capsys.readouterr().out)["files"]
    report = json.loads(open(files["json"], encoding="utf-8").read())
    with open(files["output"], "rb") as f:
        assert report["output_sha256"] == hashlib.sha256(f.read()).hexdigest()
    assert "output_sha256" not in files            # files は今までどおりパスだけ
