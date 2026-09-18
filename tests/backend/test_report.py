import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

SCRIPT = Path(__file__).resolve().parents[2] / "public/skills/hazard-report/scripts/report_generator.py"


def test_report_generates_local_files_without_upload(tmp_path):
    data = {"submitter": "Tester", "inspect_time": "2026-09-18", "report_id": "test",
            "image_url_map": {}, "scene_keywords": [], "severe_count": 0, "general_count": 0,
            "overall_evaluation": "Pending verification", "severe_hazards": [], "general_hazards": [],
            "improvement_items": [], "next_steps_reminder": []}
    source = tmp_path / "input.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), "--input-file", str(source),
                             "--format", "html", "--name", "report"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    outputs = json.loads(result.stdout)
    assert [Path(f["path"]).name for f in outputs["files"]] == ["report.html", "report.json"]
    assert (tmp_path / "report.html").is_file()
    assert json.loads((tmp_path / "report.json").read_text()) == data
    assert "publish_attachment" in outputs["delivery"]


def test_report_embeds_prefetched_images_without_network(tmp_path):
    spec = importlib.util.spec_from_file_location("report_generator", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    image = tmp_path / "photo.png"
    Image.new("RGB", (4, 4)).save(image)
    ref = module.ReportGenerator._resolve_photo_ref("photo", {"photo": str(image)})
    assert ref.startswith("data:image/png;base64,")
    assert module._get_image_source(ref).read() == image.read_bytes()
