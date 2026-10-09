"""静态安全审查引擎（阶段 3）。

**定位与纪律（重要）**
- 本模块是**风险发现工具**，输出的是"在哪个文件的哪一行命中了什么模式、为什么值得警惕"。
- 它**不是安全控制**：项目**不依赖**本模块来"放行"任何代码。真正的安全边界是
  "默认不自动执行任何安装脚本 + 有副作用操作必须经用户在受信任网页明确授权"（阶段 5）。
- 因此本模块**永远不会**输出「绝对安全」「无恶意代码」这类结论。报告只陈述
  扫描覆盖范围与命中情况，并始终携带免责声明。
- 静态模式匹配必然存在漏报：未命中**不代表**安全。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import (
    EvidenceLevel,
    Finding,
    FindingCategory,
    RiskLevel,
    SecurityReport,
)
from app.security import sanitize_for_log

_MAX_SNIPPET = 200
_SEVERITY_ORDER = {
    RiskLevel.NONE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


@dataclass(frozen=True)
class ScannedFile:
    """待扫描的一个文本文件。"""

    path: str
    content: str


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    category: FindingCategory
    severity: RiskLevel
    pattern: re.Pattern[str]
    reason: str


def _rule(
    rule_id: str,
    category: FindingCategory,
    severity: RiskLevel,
    pattern: str,
    reason: str,
) -> _Rule:
    return _Rule(rule_id, category, severity, re.compile(pattern, re.IGNORECASE), reason)


# --------------------------------------------------------------------------- #
# 规则集（检测启发式，非放行控制）
# --------------------------------------------------------------------------- #

_RULES: tuple[_Rule, ...] = (
    # --- 动态下载并执行（最高危：内容不可控、版本不固定）---
    _rule(
        "DL001", FindingCategory.DYNAMIC_DOWNLOAD_EXECUTE, RiskLevel.CRITICAL,
        r"\b(curl|wget|invoke-webrequest|iwr)\b[^\n]{0,200}?\|[^\n]{0,40}?\b(sh|bash|zsh|python|node|iex|invoke-expression)\b",
        "通过网络下载内容后直接交给解释器执行：内容与版本均不可控，等于执行远端任意代码。",
    ),
    _rule(
        "DL002", FindingCategory.DYNAMIC_DOWNLOAD_EXECUTE, RiskLevel.CRITICAL,
        r"\b(iex|invoke-expression)\b\s*\(\s*(new-object\s+net\.webclient|invoke-webrequest|iwr)",
        "PowerShell 下载即执行（IEX + WebClient/IWR）：等价于运行未经验证的远端脚本。",
    ),
    _rule(
        "DL003", FindingCategory.DYNAMIC_DOWNLOAD_EXECUTE, RiskLevel.HIGH,
        r"\b(pip|pip3)\s+install\s+(git\+https?://|https?://)|npm\s+install\s+https?://",
        "从任意 URL 安装依赖：绕过了包仓库的审计与版本锁定，供应链不可控。",
    ),
    _rule(
        "DL004", FindingCategory.DYNAMIC_DOWNLOAD_EXECUTE, RiskLevel.HIGH,
        r"\bnpx\s+(-y|--yes)\b",
        "以「免确认」方式执行远端包：会在无提示的情况下运行未固定版本的代码。",
    ),
    # --- shell / PowerShell 执行 ---
    _rule(
        "SH001", FindingCategory.SHELL_EXECUTION, RiskLevel.HIGH,
        r"\bos\.system\s*\(|\bsubprocess\.\w+\s*\([^)]*shell\s*=\s*True|\bchild_process\.exec\w*\s*\(|\bos\.popen\s*\(",
        "通过 shell 执行字符串命令：若字符串包含外部输入，将导致命令注入。",
    ),
    _rule(
        "SH002", FindingCategory.SHELL_EXECUTION, RiskLevel.MEDIUM,
        r"\beval\s*\(|\bexec\s*\(|\bnew\s+Function\s*\(",
        "动态执行代码字符串：可被用于执行任意代码，难以静态审计。",
    ),
    _rule(
        "SH003", FindingCategory.INSTALL_SCRIPT, RiskLevel.MEDIUM,
        r"\b(rm\s+-rf|rm\s+-fr|sudo\s+rm)\b",
        "安装/配置脚本中包含递归强制删除，误操作或参数注入会不可逆地删除数据。",
    ),
    # --- 文件系统写入 / 删除 ---
    _rule(
        "FS001", FindingCategory.FILESYSTEM_DELETE, RiskLevel.HIGH,
        r"\brm\s+-rf?\s+(/|~|\$HOME|/\*|\*)|\bshutil\.rmtree\s*\(\s*['\"](/|~)|remove-item\s+-recurse\s+-force",
        "对根目录 / 家目录执行递归删除：可能清空用户数据。",
    ),
    _rule(
        "FS002", FindingCategory.FILESYSTEM_WRITE, RiskLevel.MEDIUM,
        r"(>>?\s*(/etc/|~/\.bashrc|~/\.zshrc|~/\.profile|\$HOME/\.bashrc))|(open\s*\(\s*['\"](/etc/|~/\.))",
        "写入系统级或 shell 启动配置文件：会改变系统/终端行为并可能实现持久化。",
    ),
    # --- 凭据访问 ---
    _rule(
        "CR001", FindingCategory.CREDENTIAL_ACCESS, RiskLevel.CRITICAL,
        r"(~/\.ssh/id_(rsa|ed25519|ecdsa))|(\.aws/credentials)|(\.netrc)|(\.git-credentials)|(id_rsa\b)",
        "读取 SSH 私钥 / 云凭据 / 凭据文件：属于高敏感机密，绝不应被第三方插件读取。",
    ),
    _rule(
        "CR002", FindingCategory.CREDENTIAL_ACCESS, RiskLevel.HIGH,
        r"(GITHUB_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID|PRIVATE_KEY)\b",
        "访问常见 API Key / 密钥类环境变量：存在被外传或落盘的风险。",
    ),
    # --- 外联 ---
    _rule(
        "NET001", FindingCategory.NETWORK_OUTBOUND, RiskLevel.MEDIUM,
        r"\b(nc|netcat)\s+-e\b|\bcurl\b[^\n]{0,120}?\s(-d|--data|-F|--form)\b[^\n]{0,120}?(https?://)",
        "向外部端点回传数据或建立反向 shell：存在数据外泄风险。",
    ),
    _rule(
        "NET002", FindingCategory.NETWORK_OUTBOUND, RiskLevel.LOW,
        r"\brequests\.(post|put)\s*\(|\bhttp\.request\b|\bfetch\s*\(",
        "存在出站网络请求：需确认目标域名与发送内容是否合理。",
    ),
    # --- 安装后自动执行 ---
    _rule(
        "AU001", FindingCategory.POST_INSTALL_AUTORUN, RiskLevel.HIGH,
        r"\"(pre|post)install\"\s*:|\bsetup_requires\b|\binstall_requires\b.*\bexec",
        "声明了安装期自动执行的钩子：安装动作本身会触发未审计的代码。",
    ),
    # --- 持久化 ---
    _rule(
        "PS001", FindingCategory.PERSISTENCE, RiskLevel.HIGH,
        r"\bcrontab\b|\bsystemctl\s+(enable|start)\b|/etc/systemd/system|LaunchAgents|LaunchDaemons|\\\\Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Run",
        "创建计划任务 / 系统服务 / 开机启动项：属于持久化行为，安装后仍会持续运行。",
    ),
    # --- 依赖与供应链 ---
    _rule(
        "DP001", FindingCategory.DEPENDENCY_RISK, RiskLevel.LOW,
        r"--extra-index-url|--trusted-host|\bgit\+ssh://|\bindex-url\s+http://",
        "使用非官方包源或明文 HTTP 源：存在依赖被替换（供应链投毒）的风险。",
    ),
)


def review_files(
    files: list[ScannedFile],
    *,
    coverage: str = "partial",
    pinned_ref: str | None = None,
    notes: list[str] | None = None,
) -> SecurityReport:
    """扫描给定文件并生成报告。

    ``coverage``：``none`` / ``partial`` / ``full``，表示本次扫描覆盖了仓库的多少内容。
    """
    findings: list[Finding] = []
    scanned: list[str] = []
    skipped: list[str] = []
    seen: set[tuple[str, str, int]] = set()

    for item in files:
        if not isinstance(item.content, str):
            skipped.append(item.path)
            continue
        scanned.append(item.path)
        for lineno, line in enumerate(item.content.splitlines(), start=1):
            if not line.strip():
                continue
            for rule in _RULES:
                if not rule.pattern.search(line):
                    continue
                key = (item.path, rule.rule_id, lineno)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    Finding(
                        rule_id=rule.rule_id,
                        category=rule.category,
                        severity=rule.severity,
                        file=item.path,
                        line=lineno,
                        snippet=sanitize_for_log(line, max_len=_MAX_SNIPPET),
                        reason=rule.reason,
                        evidence=EvidenceLevel.VERIFIED,
                    )
                )

    risk = RiskLevel.NONE
    for finding in findings:
        if _SEVERITY_ORDER[finding.severity] > _SEVERITY_ORDER[risk]:
            risk = finding.severity

    vetoed = any(f.severity is RiskLevel.CRITICAL for f in findings)

    final_notes = list(notes or [])
    if not scanned:
        final_notes.append("本次没有可扫描的文本文件，结论为【未检查】，不代表无风险。")
    if skipped:
        final_notes.append(f"有 {len(skipped)} 个文件因不可读或非文本被跳过。")
    if not findings and scanned:
        final_notes.append(
            f"在已扫描的 {len(set(scanned))} 个文件中未命中任何已知模式；"
            "这仅说明未命中，不代表不存在风险。"
        )
    # 关键：无论是否命中，报告都必须携带免责声明（模块契约）。
    final_notes.append(SecurityReport.DISCLAIMER)

    return SecurityReport(
        scanned_files=sorted(set(scanned)),
        skipped_files=sorted(set(skipped)),
        findings=findings,
        risk_level=risk,
        vetoed=vetoed,
        coverage=coverage if scanned else "none",
        pinned_ref=pinned_ref,
        generated_at=datetime.now(timezone.utc),
        notes=final_notes,
    )