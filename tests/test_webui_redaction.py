from webui.webui import format_command_for_log


def test_format_command_for_log_redacts_sensitive_flags():
    log = format_command_for_log([
        "python",
        "-m",
        "stream_translator_gpt",
        "https://example.test/watch",
        "--openai_api_key",
        "sk-secret",
        "--google_api_key=google-secret",
        "--telegram_token",
        "telegram-secret",
        "--cqhttp_token=cqhttp-secret",
        "--discord_webhook_url",
        "https://discord.com/api/webhooks/secret",
        "--model",
        "small",
    ])

    assert "sk-secret" not in log
    assert "google-secret" not in log
    assert "telegram-secret" not in log
    assert "cqhttp-secret" not in log
    assert "discord.com/api/webhooks/secret" not in log
    assert "--openai_api_key ***" in log
    assert "--google_api_key=***" in log
    assert "--model small" in log


def test_format_command_for_log_redacts_proxy_credentials():
    log = format_command_for_log([
        "stream-translator-gpt",
        "https://example.test/watch",
        "--proxy",
        "http://user:pass@127.0.0.1:7890",
        "--input_proxy=https://proxy-user:proxy-pass@example.com:8443/path",
        "http://viewer:viewer-pass@example.org/path?a=b",
    ])

    assert "user:pass" not in log
    assert "proxy-user:proxy-pass" not in log
    assert "viewer:viewer-pass" not in log
    assert "http://***@127.0.0.1:7890" in log
    assert "--input_proxy=https://***@example.com:8443/path" in log
    assert "http://***@example.org/path?a=b" in log
