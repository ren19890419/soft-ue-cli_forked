"""Tests for cli/soft_ue_cli/skills — skill discovery and retrieval."""

from __future__ import annotations

from pathlib import Path

import pytest

from soft_ue_cli.skills import get_skill, list_skills
from soft_ue_cli.__main__ import build_parser, cmd_skills

# -- list_skills ---------------------------------------------------------------

def test_list_skills_returns_nonempty():
    skills = list_skills()
    assert len(skills) > 0

def test_list_skills_items_have_name_and_description():
    for skill in list_skills():
        assert "name" in skill
        assert "description" in skill
        assert isinstance(skill["name"], str)
        assert isinstance(skill["description"], str)

def test_list_skills_contains_blueprint_to_cpp():
    names = [s["name"] for s in list_skills()]
    assert "blueprint-to-cpp" in names
    assert "test-tools" in names
    assert "replay-changes" in names
    assert "author-test" in names
    assert "author-regression-test" in names
    assert "author-anim-state-test" in names
    assert "author-bp-parity-test" in names
    assert "author-invariant-test" in names
    assert "author-umg" in names
    assert "author-umg-designer" not in names
    assert "author-umg-workflow" not in names

# -- get_skill -----------------------------------------------------------------

def test_get_skill_returns_content():
    content = get_skill("blueprint-to-cpp")
    assert content is not None
    assert len(content) > 0
    assert "blueprint-to-cpp" in content
    assert "Dependency-first planning" in content
    assert "soft-ue-cli query-enum" in content
    assert "soft-ue-cli query-struct" in content

def test_test_tools_contains_idempotent_teardown_and_insights_stop():
    content = get_skill("test-tools")
    assert content is not None
    assert "When a new CLI tool, MCP-exposed tool, or new inspect/diff section is added" in content
    assert "already removed (treated as pass)" in content
    assert "auto-stopped (treated as pass)" in content
    assert 'encoding="utf-8"' in content
    assert "open test level retry" in content
    assert "save-asset (test level before restore)" in content
    assert "asset inspect-file summary" in content
    assert "asset inspect-file properties" in content
    assert "asset_to_disk_path" in content
    assert "project_directory" in content
    assert "asset diff-file summary" in content
    assert "asset diff-file properties" in content
    assert "save-asset blueprint" in content
    assert "shutil.copy2" in content
    assert "apply-widget-tree UMG designer spec" in content
    assert "inspect-widget-blueprint applied tree" in content
    assert "wire-widget-navigation UMG nav contract" in content
    assert "verify-umg-workflow preview widget" in content
    assert "commands json metadata" in content
    assert "mcp surface status" in content
    assert "runtime readiness help" in content
    assert "runtime binary plan install help" in content
    assert "runtime smoke plan help" in content
    assert "commands plugin mutable json" in content
    assert "commands category capture" in content
    assert "commands category mutable" in content
    assert "commands category statetree" in content
    assert "commands category animation" in content
    assert "commands category asset" in content
    assert "commands category blueprint" in content
    assert "capture viewport help" in content
    assert "capture screenshot help" in content
    assert "mutable graph add-node help" in content
    assert "statetree inspect help" in content
    assert "anim rewind status help" in content
    assert "asset preview help" in content
    assert "blueprint graph inspect help" in content
    assert "umg layout compare smoke" in content
    assert "umg preview help" in content
    assert "umg workflow help" in content

def test_test_tools_contains_session_suite():
    """The session leaves must be exercised by the integration script.

    This suite has never been run end-to-end (the `session` bridge tool is
    committed but unbuilt), so these string assertions are the only thing
    keeping the coverage from silently disappearing.
    """
    content = get_skill("test-tools")
    assert content is not None
    assert 'begin_suite("session")' in content
    assert 'run_cli("session announce"' in content
    assert 'run_cli("session list"' in content
    assert 'run_cli("session broadcast"' in content
    assert 'run_cli("session ask"' in content
    assert 'run_cli("session answer"' in content
    assert 'run_cli("session inbox"' in content
    assert 'run_cli("session leave"' in content
    # Re-runnable without cleanup: leaving twice must still pass.
    assert 'run_cli("session leave repeat (idempotent)"' in content
    # Identity is per-command, so every leaf carries --as.
    assert content.count('"--as", SESSION_NAME') >= 7

def test_test_tools_contains_config_suite():
    content = get_skill("test-tools")
    assert content is not None
    # Bridge tools
    assert "get-config-value" in content
    assert "set-config-value" in content
    assert "validate-config-key" in content
    # CLI subcommands
    assert 'run_cli("config tree"' in content
    assert 'run_cli("config get search"' in content
    assert 'run_cli("config set user layer"' in content
    assert 'run_cli("config diff audit"' in content
    assert 'run_cli("config audit"' in content
    assert "OfflineSearchKey_" in content

def test_test_tools_contains_new_automation_features():
    content = get_skill("test-tools")
    assert content is not None
    assert "advanced-automation" in content
    assert "run-python-script helper import" in content
    assert "from soft_ue_bridge import call" in content
    assert "batch-call pie/query/logs smoke" in content
    assert "call-function transient native" in content

def test_get_skill_nonexistent_returns_none():
    assert get_skill("nonexistent-skill-xyz") is None

def test_get_skill_path_traversal_returns_none():
    assert get_skill("../../../README") is None
    assert get_skill("..\\..\\README") is None
    assert get_skill(".hidden") is None

def test_get_skill_content_has_frontmatter():
    content = get_skill("blueprint-to-cpp")
    assert content.startswith("---")

def test_replay_changes_skill_mentions_bundle_workflow():
    content = get_skill("replay-changes")
    assert content is not None
    assert "Git workflow" in content
    assert "Perforce workflow" in content
    assert "git show :1:" in content
    assert "p4 sync" in content

def test_author_test_skill_mentions_supported_subskills():
    content = get_skill("author-test")
    assert content is not None
    assert "Use a two-layer workflow:" in content
    assert "Exploration layer: CLI + bridge + Python scripts" in content
    assert "Committed layer: C++ Automation Spec tests" in content
    assert "author-regression-test" in content
    assert "author-anim-state-test" in content
    assert "author-bp-parity-test" in content
    assert "author-invariant-test" in content
    assert "C++ scaffold" in content

def test_authoring_subskills_target_cpp_committed_tests():
    regression = get_skill("author-regression-test")
    anim = get_skill("author-anim-state-test")
    parity = get_skill("author-bp-parity-test")
    invariant = get_skill("author-invariant-test")

    assert regression is not None
    assert anim is not None
    assert parity is not None
    assert invariant is not None

    assert "committed C++ gameplay regression test" in regression
    assert "Source/<Project>Tests/Private/Regression/TEST_<Slug>.cpp" in regression
    assert "committed C++ regression test for animation behavior" in anim
    assert "Source/<Project>Tests/Private/Anim/TEST_<Slug>.cpp" in anim
    assert "committed C++ Automation Spec" in parity
    assert "CLI capture is acceptable here as exploration tooling" in parity
    assert "single-property invariant" in invariant
    assert "Source/<Project>Tests/Private/Invariants/TEST_<Slug>.cpp" in invariant

def test_author_umg_skill_targets_designer_navigation_and_runtime_verification():
    content = get_skill("author-umg")
    assert content is not None
    assert "soft-ue-cli umg designer apply" in content
    assert "CanvasPanel" in content
    assert "WidgetSwitcher" in content
    assert "umg designer inspect" in content
    assert "region/bounding-box table" in content
    assert "visual-fidelity checklist" in content
    assert "1920x1080" in content
    assert "umg layout compare --mode pixel" in content
    assert "umg_expected_layout.json" in content
    assert "placeholder asset manifest" in content
    assert "umg layout compare" in content
    assert "soft-ue-cli umg navigation wire" in content
    assert "soft-ue-cli umg verify navigation" in content
    assert "stable widget-name contract" in content
    assert "click_sequence" in content

def test_session_protocol_skill_is_listed():
    assert "session-protocol" in {skill["name"] for skill in list_skills()}

def test_session_protocol_skill_states_silence_is_not_consent():
    body = get_skill("session-protocol")

    assert body is not None
    assert "session announce" in body
    assert "silence is not consent" in body.lower()
    assert "stale" in body

def test_session_protocol_skill_names_the_real_roster_field():
    """The liveness grade ships as `state`, not `liveness` (RecordToJson).

    A skill that names a field the bridge does not emit sends the agent looking
    for a key that is never there.
    """
    body = get_skill("session-protocol")

    assert "`state`" in body
    for grade in ("active", "idle", "stale", "ended"):
        assert grade in body
    assert "`liveness`" not in body

def test_session_protocol_skill_warns_identity_is_per_command():
    """`export` does not persist between Claude Code Bash calls."""
    body = get_skill("session-protocol")

    assert "export" in body
    assert "SOFT_UE_SESSION=" in body
    assert "--as" in body

def test_session_protocol_skill_scopes_the_as_flag_to_session_commands():
    """`--as` is on the seven `session` leaves only.

    Both worked examples used to be `session list`, so an agent generalizing
    from them writes `pie-session start --as <name>` — an argparse error. The
    skill must show the env prefix on a non-session command instead.
    """
    body = get_skill("session-protocol")

    assert "SOFT_UE_SESSION=cape-cloth soft-ue-cli pie-session start" in body
    assert "only" in body and "`session` leaves" in body
    # No example may teach `--as` on a non-session command.
    for line in body.splitlines():
        if line.startswith("SOFT_UE_SESSION=") or line.startswith("soft-ue-cli "):
            assert "--as" not in line, f"example mixes identity forms: {line}"

def test_session_protocol_skill_names_the_cost_of_mixing_identity_forms():
    """A split identity is silent: the `pie` claim lands on a nameless row."""
    body = get_skill("session-protocol")

    assert "unknown:" in body
    assert "session inbox --as cape-cloth" in body
    assert "session leave --as cape-cloth" in body

def test_session_protocol_skill_separates_derived_from_declared():
    body = get_skill("session-protocol")

    assert "pie-session" in body
    assert "derived" in body.lower()
    # Destruction that bypasses the bridge is invisible to the channel.
    assert "taskkill" in body
    assert "Build.bat" in body

# -- skill file validation -----------------------------------------------------

def test_all_skills_have_required_frontmatter():
    """Every .md skill file must have name, description, and version in frontmatter."""
    skills_dir = Path(__file__).parents[2] / "cli" / "soft_ue_cli" / "skills"
    for md_file in skills_dir.glob("*.md"):
        text = md_file.read_text(encoding="utf-8")
        assert text.startswith("---"), f"{md_file.name} missing frontmatter"
        end = text.find("---", 3)
        assert end != -1, f"{md_file.name} missing closing frontmatter fence"
        front = text[3:end]
        assert "name:" in front, f"{md_file.name} missing name"
        assert "description:" in front, f"{md_file.name} missing description"
        assert "version:" in front, f"{md_file.name} missing version"

# -- CLI argument parsing ------------------------------------------------------

def test_parser_skills_list():
    parser = build_parser()
    args = parser.parse_args(["skills", "list"])
    assert args.command == "skills"
    assert args.skills_action == "list"

def test_parser_skills_get():
    parser = build_parser()
    args = parser.parse_args(["skills", "get", "blueprint-to-cpp"])
    assert args.command == "skills"
    assert args.skills_action == "get"
    assert args.skill_name == "blueprint-to-cpp"

def test_cmd_skills_list_prints_output(capsys):
    args = build_parser().parse_args(["skills", "list"])
    cmd_skills(args)
    out = capsys.readouterr().out
    assert "blueprint-to-cpp" in out

def test_cmd_skills_get_prints_content(capsys):
    args = build_parser().parse_args(["skills", "get", "blueprint-to-cpp"])
    cmd_skills(args)
    out = capsys.readouterr().out
    assert "---" in out
    assert "name: blueprint-to-cpp" in out

def test_cmd_skills_get_nonexistent_exits():
    args = build_parser().parse_args(["skills", "get", "no-such-skill"])
    with pytest.raises(SystemExit) as exc:
        cmd_skills(args)
    assert exc.value.code == 1

def test_every_skill_file_is_force_included_in_the_wheel():
    """Hatchling drops non-.py files unless listed, so a new skill ships broken
    unless pyproject's force-include names it. Nothing else catches this: the
    skill loads fine from a source checkout and only vanishes in the built wheel."""
    # Found by walking up from this file, not by importing soft_ue_cli: an
    # installed copy of the package would send this test to the wrong tree.
    # The monorepo keeps these under cli/, the synced public repo at its root,
    # and the two layouts sit at different depths — so search rather than index.
    cli_root = next(
        (d for d in Path(__file__).parents
         for d in (d / "cli", d)
         if (d / "pyproject.toml").exists() and (d / "soft_ue_cli" / "skills").is_dir()),
        None,
    )
    assert cli_root is not None, "could not locate the CLI package root from this test file"

    on_disk = {p.name for p in (cli_root / "soft_ue_cli" / "skills").glob("*.md")}
    pyproject = (cli_root / "pyproject.toml").read_text(encoding="utf-8")

    assert on_disk, f"no skill files found under {cli_root / 'soft_ue_cli' / 'skills'}"

    missing = sorted(
        name for name in on_disk
        if f'"soft_ue_cli/skills/{name}"' not in pyproject
    )

    assert not missing, (
        f"skill files absent from [tool.hatch.build.targets.wheel.force-include]: {missing}. "
        "Add one line per file or they will not ship in the wheel."
    )
