from pathlib import Path

from nanobot.gateway.route_manager import RouteManager


def test_parse_profile_command() -> None:
    assert RouteManager._parse_profile_command("/session config alpha") == "alpha"
    assert RouteManager._parse_profile_command("/session config default") == "default"
    assert RouteManager._parse_profile_command("hello") is None


def test_extract_profile_prefix() -> None:
    parsed = RouteManager._extract_profile_from_message("@config:teamA summarize this")
    assert parsed == ("teamA", "summarize this")
    assert RouteManager._extract_profile_from_message("no prefix") is None


def test_resolve_profile_config_candidates(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    dot_nanobot = home / ".nanobot"
    profiles_dir = dot_nanobot / "profiles"
    profiles_dir.mkdir(parents=True)
    cfg = profiles_dir / "agentx.json"
    cfg.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: home)

    mgr = RouteManager.__new__(RouteManager)
    resolved = mgr._resolve_profile_config("agentx")
    assert resolved == cfg


def test_resolve_profile_missing(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".nanobot").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)

    mgr = RouteManager.__new__(RouteManager)
    assert mgr._resolve_profile_config("missing") is None


def test_parse_self_rollback_command() -> None:
    sha = "abcdef1234567"
    assert RouteManager._parse_self_rollback_command(f"/self-rollback {sha}") == sha
    assert RouteManager._parse_self_rollback_command("/self-rollback not-a-sha") is None
    assert RouteManager._parse_self_rollback_command("hello") is None
