"""Config layering: tracked config.yaml + gitignored config.local.yaml + env."""
import textwrap

from voice_assistant.assistant import load_config


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(textwrap.dedent(content))
    return p


def test_local_override_merged(tmp_path):
    _write(tmp_path, "config.yaml", """
        openwebui:
          base_url: ""
          api_key: ""
          model: "llama3.2:3b"
        audio:
          card: 2
    """)
    _write(tmp_path, "config.local.yaml", """
        openwebui:
          base_url: "http://192.168.2.9:3000"
          api_key: "sk-secret"
    """)
    cfg = load_config(str(tmp_path / "config.yaml"))
    assert cfg["openwebui"]["base_url"] == "http://192.168.2.9:3000"
    assert cfg["openwebui"]["api_key"] == "sk-secret"
    assert cfg["openwebui"]["model"] == "llama3.2:3b"   # untouched by override
    assert cfg["audio"]["card"] == 2                    # untouched section


def test_env_overrides_win(tmp_path, monkeypatch):
    _write(tmp_path, "config.yaml", """
        openwebui:
          base_url: ""
          api_key: ""
    """)
    _write(tmp_path, "config.local.yaml", """
        openwebui:
          base_url: "http://from-local-file:3000"
          api_key: "from-local-file"
    """)
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://from-env:3000")
    monkeypatch.setenv("OPENWEBUI_API_KEY", "from-env")
    cfg = load_config(str(tmp_path / "config.yaml"))
    assert cfg["openwebui"]["base_url"] == "http://from-env:3000"
    assert cfg["openwebui"]["api_key"] == "from-env"


def test_no_override_still_loads(tmp_path):
    _write(tmp_path, "config.yaml", """
        openwebui:
          base_url: ""
          api_key: ""
        audio:
          card: 2
    """)
    cfg = load_config(str(tmp_path / "config.yaml"))
    assert cfg["openwebui"]["base_url"] == ""
