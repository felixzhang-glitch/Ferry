"""pre-push 密钥扫描器（.qoder/hooks/secret_scan.py）单元测试。

重要：所有假密钥必须运行时拼接构造，不能写成完整字面量，
否则本测试文件自身会被 pre-push 钩子拦下。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER_PATH = REPO_ROOT / ".qoder" / "hooks" / "secret_scan.py"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("ferry_secret_scan", SCANNER_PATH)
    assert spec and spec.loader, f"无法加载 {SCANNER_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = _load_scanner()


def rules_hit(text: str, path: str = "sample.py") -> set[str]:
    return {finding.rule for finding in scan.scan_text(path, text)}


# --- 假密钥构造（运行时拼接，避免自我阻断）-------------------------------

AWS_AK = "AKIA" + "J4XQ7ZB2MTKD5W6N"
AWS_SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYz" + "KdVtQ9Zp8"
ALI_AK = "LTAI" + "5t9Kq3ZxWvB7NmPd"
ALI_SK = "Hq7Zx9WvB3NmPdKt5" + "RgYc2LsJfA0Hi"
TENCENT_AK = "AKID" + "z8Kq3ZxWvB7NmPdRt5Yc2Ls"
OPENAI_KEY = "sk-" + "T3BlbkFJq7Zx9WvB3NmPdKt5RgYc2LsJ"
DASHSCOPE_KEY = "sk-" + "3f9a1c7e5b2d8046af13ce97b2d54e6f"
ANTHROPIC_KEY = "sk-ant-" + "api03-" + ("Zq7Xv9Wb3Nm" * 8)
GOOGLE_KEY = "AIza" + "SyDq7Zx9WvB3NmPdKt5RgYc2LsJfA0HiU4X"
GITHUB_TOKEN = "ghp_" + "q7Zx9WvB3NmPdKt5RgYc2LsJfA0HiU4Xe2Tp"
GITLAB_TOKEN = "glpat-" + "q7Zx9WvB3NmPdKt5RgYc"
FEISHU_APP_ID = "cli_" + "a1b2c3d4e5f60718"
NOTION_TOKEN = "secret_" + "q7Zx9WvB3NmPdKt5RgYc2LsJfA0HiU4Xe2TpMnBv1Qw"
SLACK_TOKEN = "xox" + "b-" + "2384710293-4837291028-Zq7Xv9Wb3NmPdKt5RgYc"
STRIPE_KEY = "sk_" + "live_" + "q7Zx9WvB3NmPdKt5RgYc2Ls"
TELEGRAM_TOKEN = "1234509876" + ":AA" + "Hq7Zx9WvB3NmPdKt5RgYc2LsJfA0HiU4X"
HF_TOKEN = "hf_" + "qZxWvBNmPdKtRgYcLsJfAHiUXeTpMnBvQw"
NPM_TOKEN = "npm_" + "q7Zx9WvB3NmPdKt5RgYc2LsJfA0HiU4Xe2Tp"
JWT = (
    "eyJ"
    + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    + ".eyJ"
    + "zdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
    + ".Zq7Xv9Wb3NmPdKt5RgYc2LsJfA0HiU4"
)
PRIVATE_KEY_HEADER = "-----BEGIN" + " RSA PRIVATE KEY" + "-----"


@pytest.mark.parametrize(
    ("text", "expected_rule"),
    [
        (f"aws_access_key_id = {AWS_AK}", "aws-access-key-id"),
        (f'aws_secret_access_key = "{AWS_SK}"', "aws-secret-access-key"),
        (f"ALIBABA_CLOUD_ACCESS_KEY_ID={ALI_AK}", "alibaba-access-key-id"),
        (f'accessKeySecret: "{ALI_SK}"', "alibaba-access-key-secret"),
        (f"secret_id = {TENCENT_AK}", "tencent-secret-id"),
        (f'OPENAI_API_KEY="{OPENAI_KEY}"', "openai-style-api-key"),
        (f"DASHSCOPE_API_KEY={DASHSCOPE_KEY}", "ferry-env-secret"),
        (f'x = "{ANTHROPIC_KEY}"', "anthropic-api-key"),
        (f"key = {GOOGLE_KEY}", "google-api-key"),
        (f"url = https://x@github.com with {GITHUB_TOKEN}", "github-token"),
        (f"CI_TOKEN={GITLAB_TOKEN}", "gitlab-pat"),
        (f"app_id = {FEISHU_APP_ID}", "feishu-app-id"),
        (f'"notion": "{NOTION_TOKEN}"', "notion-internal-token"),
        (f"slack = {SLACK_TOKEN}", "slack-token"),
        (f"stripe = {STRIPE_KEY}", "stripe-key"),
        (f"bot = {TELEGRAM_TOKEN}", "telegram-bot-token"),
        (f"hf = {HF_TOKEN}", "huggingface-token"),
        (f"npm = {NPM_TOKEN}", "npm-token"),
        (f"Authorization: Bearer {JWT}", "jwt-token"),
        (PRIVATE_KEY_HEADER, "private-key-block"),
        ("DATABASE_URL=postgres://admin:Zq7Xv9Wb3Nm@db.internal:5432/app", "uri-with-password"),
        (
            "url = https://open.feishu.cn/open-apis/bot/v2/hook/8f14e45f-ceea-467a-9c3d-1b2a7f0e5d64",
            "feishu-bot-webhook",
        ),
    ],
)
def test_detects_provider_secrets(text: str, expected_rule: str) -> None:
    assert expected_rule in rules_hit(text), f"未命中 {expected_rule}: {text[:60]}"


def test_project_env_secrets_reported_without_entropy_gate() -> None:
    for line in (
        "FEISHU_APP_SECRET=abcdefabcdefabcdef",
        "FEISHU_ENCRYPT_KEY=Zq7Xv9Wb3NmPdKt5",
        "FEISHU_VERIFICATION_TOKEN=Kt5RgYc2LsJfA0Hi",
        "WECHAT_WEBHOOK_TOKEN=Wb3NmPdKt5RgYc2L",
        "CODEX_API_KEY=NmPdKt5RgYc2LsJf",
    ):
        assert "ferry-env-secret" in rules_hit(line), line


@pytest.mark.parametrize(
    "text",
    [
        "FEISHU_APP_SECRET=",
        "FEISHU_APP_SECRET=your_app_secret_here",
        "DASHSCOPE_API_KEY=请换成一段随机字符串",
        "api_key = ${OPENAI_API_KEY}",
        "api_key = $DASHSCOPE_API_KEY",
        "token: <your-token>",
        'password = "changeme"',
        'api_key = "xxxxxxxxxxxxxxxxxxxx"',
        'secret = "placeholder-value-here"',
        'token = "REDACTED"',
        "api_key = settings.dashscope_api_key",
        'access_token = os.environ["FEISHU_TOKEN"]',
        'password = "hunter2"',
        'token = "abcdef1234567890abcdef"',
        'secret_key = "aaaaaaaaaaaaaaaaaaaaaa"',
        'api_key = "my-service-api-key-name"',
        'path_token = "/data/app/Ferry/runtime"',
        'version_token = "1.2.3-beta.20240101"',
    ],
)
def test_placeholders_and_identifiers_pass(text: str) -> None:
    assert rules_hit(text) == set(), f"占位符被误报: {text}"


def test_same_value_reported_once_per_line() -> None:
    """同一个值同时命中多条规则时只报一次，由高置信规则胜出。"""
    findings = scan.scan_text("a.py", 'password = "Zq7Xv9Wb3NmPdKt5RgYc"')
    assert len(findings) == 1

    env_findings = scan.scan_text("conf/.env", f"DASHSCOPE_API_KEY={DASHSCOPE_KEY}")
    assert [f.rule for f in env_findings] == ["ferry-env-secret"]


def test_inline_ignore_comment_suppresses_finding() -> None:
    line = f"aws_key = {AWS_AK}"
    assert rules_hit(line) != set()
    assert rules_hit(f"{line}  # secret-scan: ignore") == set()
    assert rules_hit(f"{line}  # pragma: allowlist secret") == set()


def test_skipped_paths_and_dangerous_paths() -> None:
    assert scan.path_is_skipped("package-lock.json")
    assert scan.path_is_skipped("bun.lock")
    assert scan.path_is_skipped("runtime/server/backend.json")
    assert scan.path_is_skipped("logs/ferry.log")
    assert not scan.path_is_skipped("lib/python/app/config.py")

    assert scan.dangerous_path_reason("conf/.env")
    assert scan.dangerous_path_reason(".env.local")
    assert scan.dangerous_path_reason("certs/server.pem")
    assert scan.dangerous_path_reason("secrets/id_rsa")
    assert scan.dangerous_path_reason("conf/wechat/account.json")
    assert scan.dangerous_path_reason("rules/admin.md")
    assert scan.dangerous_path_reason("conf/.env.example") is None
    assert scan.dangerous_path_reason("README.md") is None


def test_allowlist_entries() -> None:
    allowlist = scan.Allowlist()
    allowlist.parse(
        "\n".join(
            [
                "# comment",
                "path:docs/references/*.txt",
                "regex:^" + "sk-demo-",
                "",
            ]
        )
    )
    finding = scan.Finding(path="docs/references/pi-cli.txt", line_no=3, rule="x", secret="whatever")
    assert allowlist.allows(finding)

    other = scan.Finding(path="lib/a.py", line_no=1, rule="x", secret="sk-demo-" + "0123456789")
    assert allowlist.allows(other)

    blocked = scan.Finding(path="lib/a.py", line_no=1, rule="x", secret=AWS_AK)
    assert not allowlist.allows(blocked)

    fingerprint_list = scan.Allowlist()
    fingerprint_list.parse(f"fingerprint:{blocked.fingerprint}")
    assert fingerprint_list.allows(blocked)


def test_parse_diff_tracks_added_line_numbers() -> None:
    diff = "\n".join(
        [
            "diff --git a/conf/app.py b/conf/app.py",
            "index 1111111..2222222 100644",
            "--- a/conf/app.py",
            "+++ b/conf/app.py",
            "@@ -10,0 +11,2 @@ def setup():",
            "+    harmless = 1",
            f'+    aws_key = "{AWS_AK}"',
        ]
    )
    findings = scan.parse_diff(diff)
    assert len(findings) == 1
    assert findings[0].path == "conf/app.py"
    assert findings[0].line_no == 12
    assert findings[0].rule == "aws-access-key-id"


def test_parse_diff_ignores_removed_lines_and_skipped_files() -> None:
    diff = "\n".join(
        [
            "diff --git a/app.py b/app.py",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -5 +5 @@",
            f'-    aws_key = "{AWS_AK}"',
            "+    aws_key = os.environ['AWS_KEY']",
            "diff --git a/package-lock.json b/package-lock.json",
            "--- a/package-lock.json",
            "+++ b/package-lock.json",
            "@@ -1,0 +2,1 @@",
            f'+    "token": "{GITHUB_TOKEN}"',
        ]
    )
    assert scan.parse_diff(diff) == []


def test_parse_diff_flags_new_dangerous_file() -> None:
    diff = "\n".join(
        [
            "diff --git a/conf/.env b/conf/.env",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/conf/.env",
            "@@ -0,0 +1 @@",
            "+HARMLESS=1",
        ]
    )
    findings = scan.parse_diff(diff)
    assert [f.rule for f in findings] == ["dangerous-file"]
    assert findings[0].path == "conf/.env"


def test_finding_masks_secret_value() -> None:
    finding = scan.Finding(path="a.py", line_no=1, rule="aws-access-key-id", secret=AWS_AK)
    assert AWS_AK not in finding.masked
    assert finding.masked.startswith(AWS_AK[:4])
    assert len(finding.fingerprint) == 40


def test_entropy_helpers() -> None:
    assert scan.shannon_entropy("") == 0.0
    assert scan.shannon_entropy("aaaa") == 0.0
    assert scan.looks_random("Zq7Xv9Wb3NmPdKt5RgYc2LsJ")
    assert not scan.looks_random("short")
    assert not scan.looks_random("settings.dashscope_api_key")
    assert not scan.looks_random("aaaaaaaaaaaaaaaaaaaa")


def test_main_returns_zero_on_clean_files(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text("api_key = os.environ['X']\n", encoding="utf-8")
    assert scan.main(["--files", str(clean), "--quiet"]) == 0


def test_main_returns_one_on_dirty_files(tmp_path: Path) -> None:
    dirty = tmp_path / "dirty.py"
    dirty.write_text(f'aws_key = "{AWS_AK}"\n', encoding="utf-8")
    assert scan.main(["--files", str(dirty), "--quiet"]) == 1
