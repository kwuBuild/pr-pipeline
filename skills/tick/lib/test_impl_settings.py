"""Canonical implementer settings keep alias precedence and route inheritance."""

import pipeline
import pytest
import tick


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "operator.toml"
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(path))
    return path


@pytest.mark.parametrize("canonical", [True, False])
def test_impl_settings_normalize_each_layer_before_precedence(config_path, tmp_path, monkeypatch, canonical):
    first, second = ("impl_", "") if canonical else ("", "impl_")
    floor = tmp_path / "floor.toml"
    floor.write_text(f'[defaults]\n{first}model="floor"\n{first}effort="low"\n')
    monkeypatch.setattr(pipeline, "_PACKAGED_CONFIG_PATH", floor)
    config_path.write_text(
        f'[defaults]\n{second}model="defaults"\n{second}effort="medium"\n'
        f'[repos."owner/name"]\n{first}model="repo"\n{first}effort="high"\n'
    )
    for repo, overrides, expected in (
        ("other/repo", {}, ("defaults", "medium")),
        ("owner/name", {}, ("repo", "high")),
        ("owner/name", {f"{second}model": "cli", f"{second}effort": "max"}, ("cli", "max")),
    ):
        config = pipeline.resolve_config(repo, cli_overrides=overrides)
        assert config.errors == config.warnings == ()
        assert (config.model, config.effort) == expected
        assert (config.fix_model, config.fix_effort) == expected


@pytest.mark.parametrize("layer", ["floor", "defaults", "repo", "cli"])
@pytest.mark.parametrize("key, value, other", [("model", "sonnet", "opus"), ("effort", "high", "low")])
@pytest.mark.parametrize("conflict", [False, True])
def test_impl_settings_same_layer_duplicates(config_path, tmp_path, monkeypatch, layer, key, value, other, conflict):
    pair = {key: value, f"impl_{key}": other if conflict else f" {value} "}
    text = "\n".join(f'{k}="{v}"' for k, v in pair.items())
    floor = tmp_path / "floor.toml"
    floor.write_text("[defaults]\n" + (text if layer == "floor" else ""))
    monkeypatch.setattr(pipeline, "_PACKAGED_CONFIG_PATH", floor)
    config_path.write_text(
        ("[defaults]\n" + text if layer == "defaults" else
         '[repos."owner/name"]\n' + text if layer == "repo" else "")
    )
    config = pipeline.resolve_config("owner/name", cli_overrides=pair if layer == "cli" else {})
    if conflict:
        assert any("conflict" in e and f"impl_{key}" in e for e in config.errors)
        assert any("conflict" in e for e in config.tuning_errors)
    else:
        assert config.errors == config.warnings == ()
        assert getattr(config, key) == value


@pytest.mark.parametrize("settings, expected", [
    ('', ("sonnet", "high")),
    ('fix_engine="claude-ds"', ("", "high")),
    ('fix_engine="claude-ds"\nfix_model="deepseek-custom"\nfix_effort="low"', ("deepseek-custom", "low")),
    ('fix_model="opus"\nfix_effort="max"', ("opus", "max")),
])
def test_impl_settings_preserve_fixer_inheritance(config_path, settings, expected):
    config_path.write_text('[defaults]\nimpl_model="sonnet"\nimpl_effort="high"\n' + settings)
    config = pipeline.resolve_config("owner/name")
    assert config.errors == config.warnings == ()
    assert (config.fix_model, config.fix_effort) == expected


@pytest.mark.parametrize("mode", [[], ["#7"], ["pr:7"], ["--loop"], ["--loop", "--only", "pr:7"]])
@pytest.mark.parametrize("equals", [False, True])
def test_impl_settings_cli_aliases_reach_shared_parser(mode, equals):
    flags = ["--impl-model=sonnet", "--impl-effort=high"] if equals else ["--impl-model", "sonnet", "--impl-effort", "high"]
    parsed = tick.parse_cli_args([*mode, *flags, "--model", "sonnet", "--effort", "high"])
    assert (parsed["model"], parsed["effort"]) == ("sonnet", "high")
    assert parsed["loop"] == ("--loop" in mode)


@pytest.mark.parametrize("key, values", [("model", ("sonnet", "opus")), ("effort", ("high", "low"))])
@pytest.mark.parametrize("reverse", [False, True])
def test_impl_settings_cli_refuses_conflicting_aliases(key, values, reverse, capsys):
    flags = [f"--impl-{key}", f"--{key}"]
    if reverse:
        flags.reverse()
    with pytest.raises(SystemExit):
        tick.parse_cli_args([flags[0], values[0], flags[1], values[1]])
    assert "conflict" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--impl-model", "--impl-effort"])
@pytest.mark.parametrize("suffix", [[], ["--loop"], ["="]])
def test_impl_settings_cli_requires_a_value(flag, suffix):
    args = [flag + "="] if suffix == ["="] else [flag, *suffix]
    with pytest.raises(SystemExit):
        tick.parse_cli_args(args)
