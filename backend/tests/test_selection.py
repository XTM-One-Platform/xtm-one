"""The merge queue's test selection (``tests/selection.py``): each rule on a small tree, then the real one."""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest
import yaml

from tests import selection
from tests.selection import (
    FrontendUnit,
    Graph,
    e2e_projects,
    path_pieces,
    read_selected_files,
    select_backend,
    select_frontend_unit,
    shard_list,
)


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return root


@pytest.fixture
def graph(tmp_path: Path) -> Graph:
    backend = _tree(
        tmp_path / "backend",
        {
            "app/__init__.py": "",
            "app/main.py": "from app.api import routes\n",
            "app/api/__init__.py": "",
            "app/api/routes.py": "from app.services import billing, render\n",
            "app/services/__init__.py": "",
            "app/services/billing.py": "from app.core import money\n",
            "app/services/render.py": """
                from typing import TYPE_CHECKING
                if TYPE_CHECKING:
                    from app.services import billing
                def go():
                    from app.main import routes  # a function's import: one step only
            """,
            "app/services/seeds.py": 'FOLDER = "persona"\n',
            "app/core/__init__.py": "",
            "app/core/money.py": "RATE = 1\n",
            "app/data/persona/one.md": "hello\n",
            "app/data/orphan/two.md": "nobody reads me\n",
            "app/services/AGENTS.md": "how the services work\n",
            "tests/__init__.py": "",
            "tests/helpers.py": "from app.services import render\n",
            "tests/test_billing.py": "from app.services.billing import money\n",
            "tests/test_render.py": "from tests.helpers import render\n",
            "tests/test_patch.py": 'PATCHED = "app.core.money.RATE"\n',
            "tests/test_main.py": "from app.main import routes\n",
            "tests/test_tree.py": "from tests import app_sources\n",
            "tests/test_walker.py": 'APP = ROOT / "app"\nFILES = sorted(APP.rglob("*.py"))\n',
            "tests/test_frontend_catalog.py": 'CATALOG = ROOT / "frontend" / "src"\n',
            "tests/test_seeds.py": "from app.services import seeds\n",
            "tests/test_gone.py": "from app.services import gone\n",
            "alembic/versions/0001_rate.py": "def upgrade():\n    from app.core.money import RATE\n",
            "app/core/migrations.py": "def run_migrations():\n    from app.core import indexes\n",
            "app/core/indexes.py": "",
            "tests/test_reads_seeds.py": 'SOURCE = (ROOT / "services" / "seeds.py").read_text()\n',
            "tests/test_globs_persona.py": 'TEXTS = sorted((ROOT / "data" / "persona").glob("*.md"))\n',
            "tests/test_app_file_walker.py": 'import app\nFILES = Path(app.__file__).parent.rglob("*.py")\n',
            "tests/test_scans_tests.py": 'FILES = sorted(HERE.glob("test_*.py"))\n',
            "tests/frozen.py": "NOW = 1\n",
            "tests/conftest.py": "from tests.frozen import NOW\n",
            "scripts/dump.py": "def render():\n    from app.core import money\n",
            "tests/test_dump.py": "from scripts.dump import render\n",
            "alembic/env.py": "from app.core import config\n",
            "app/core/config.py": "def check():\n    from app.core import urls\n",
            "app/core/urls.py": "from app.core import hosts\n",
            "app/core/hosts.py": "def resolve():\n    from app.services import billing\n",
            "tests/test_db_postgres.py": "import pytest\npytestmark = [pytest.mark.integration]\n",
            "tests/test_mixed.py": "import pytest\n@pytest.mark.integration\ndef test_db(): ...\n",
        },
    )
    return Graph.read(backend)


def _files(changed: list[str], graph: Graph) -> set[str]:
    picked = select_backend(changed, graph)
    assert picked.full is None, picked.full
    return set(picked.files)


def test_a_module_selects_the_tests_that_import_it_at_any_depth(graph):
    files = _files(["backend/app/core/money.py"], graph)
    assert {"tests/test_billing.py", "tests/test_patch.py", "tests/test_main.py"} <= files
    assert "tests/test_render.py" not in files


def test_a_function_import_counts_one_step_from_the_change_only(graph):
    # render imports app.main inside a function: a change to app.main reaches
    # render's importers, but a change to money does not travel back through it.
    assert "tests/test_render.py" in _files(["backend/app/main.py"], graph)
    assert "tests/test_render.py" not in _files(["backend/app/services/billing.py"], graph)


def test_a_type_checking_import_reaches_nothing(graph):
    assert graph.names["app/services/render.py"] == set()


def test_a_test_helper_carries_its_imports_to_the_tests_that_use_it(graph):
    assert "tests/test_render.py" in _files(["backend/app/services/render.py"], graph)
    assert "tests/test_render.py" in _files(["backend/tests/helpers.py"], graph)


def test_a_changed_test_file_runs_with_the_tests_that_walk_the_test_files(graph):
    assert _files(["backend/tests/test_billing.py"], graph) == {"tests/test_billing.py", "tests/test_scans_tests.py"}


def test_a_module_runs_the_tests_that_read_its_source_by_path(graph):
    assert "tests/test_reads_seeds.py" in _files(["backend/app/services/seeds.py"], graph)
    assert "tests/test_reads_seeds.py" not in _files(["backend/app/services/billing.py"], graph)


def test_a_test_that_imports_a_script_reaches_what_the_script_imports(graph):
    assert "tests/test_dump.py" in _files(["backend/app/core/money.py"], graph)
    assert "tests/test_dump.py" in _files(["backend/scripts/dump.py"], graph)


def test_a_tree_reader_runs_on_any_app_module_and_only_then(graph):
    # Through app_sources, or a walk of the directory it names "app" (a tripwire's APP.rglob).
    walkers = {"tests/test_tree.py", "tests/test_walker.py", "tests/test_app_file_walker.py"}
    assert walkers <= _files(["backend/app/services/seeds.py"], graph)
    assert "tests/test_tree.py" not in _files(["frontend/src/App.tsx"], graph)


def test_a_file_outside_backend_selects_the_tests_that_name_its_area(graph):
    assert _files(["frontend/src/App.tsx"], graph) == {"tests/test_frontend_catalog.py"}
    assert _files(["docs/docs/index.md"], graph) == set()


def test_a_data_file_reaches_the_tests_of_the_module_that_names_its_folder(graph):
    assert "tests/test_seeds.py" in _files(["backend/app/data/persona/one.md"], graph)


def test_a_data_file_runs_the_tests_that_glob_its_folder(graph):
    assert "tests/test_globs_persona.py" in _files(["backend/app/data/persona/one.md"], graph)


def test_a_data_file_no_module_names_runs_everything(graph):
    assert select_backend(["backend/app/data/orphan/two.md"], graph).full


def test_a_markdown_file_beside_a_package_s_modules_is_its_documentation(graph):
    # No module names it: it reaches no test, rather than every module naming its directory.
    assert _files(["backend/app/services/AGENTS.md"], graph) == set()


def test_a_deleted_module_reaches_what_still_names_it_and_no_more(graph):
    files = _files(["backend/app/services/gone.py", "backend/tests/test_removed.py"], graph)
    # The tree readers run too, as a deletion changes the tree they read, and the test walkers the tests they read.
    walkers = {
        "tests/test_tree.py",
        "tests/test_walker.py",
        "tests/test_app_file_walker.py",
        "tests/test_scans_tests.py",
    }
    assert files == {"tests/test_gone.py", *walkers}


@pytest.mark.parametrize(
    "path",
    [
        "backend/tests/conftest.py",
        "backend/tests/frozen.py",  # a helper conftest loads: an autouse fixture applies it to every test
        "backend/uv.lock",
        ".github/workflows/ci.yml",
        "backend/tests/selection.py",
    ],
)
def test_what_every_test_runs_under_runs_everything(graph, path):
    assert select_backend([path], graph).full


def test_a_database_input_runs_the_integration_suite_whole(graph):
    picked = select_backend(["backend/alembic/versions/x.py"], graph)
    assert picked.full is None and "integration" in picked.full_suites


def test_migrations_run_for_what_the_chain_imports_and_loads(graph, tmp_path):
    alembic = tmp_path / "backend" / "alembic"
    # A migration body imports money inside upgrade(): its change runs the chain, billing's does not.
    assert selection.migrations_reason(["backend/app/core/money.py"], graph, alembic)
    assert selection.migrations_reason(["backend/alembic/versions/0002.py"], graph, alembic)
    assert selection.migrations_reason(["backend/app/services/billing.py"], graph, alembic) is None


def test_migrations_run_for_what_a_loaded_module_s_functions_import_one_step(graph, tmp_path):
    # config's validator imports urls while env.py loads config: urls and what it loads run the chain,
    # hosts's own function import does not travel further.
    alembic = tmp_path / "backend" / "alembic"
    assert selection.migrations_reason(["backend/app/core/urls.py"], graph, alembic)
    assert selection.migrations_reason(["backend/app/core/hosts.py"], graph, alembic)
    assert selection.migrations_reason(["backend/app/services/billing.py"], graph, alembic) is None


def test_migrations_run_for_the_actions_that_install_their_interpreter(graph, tmp_path):
    alembic = tmp_path / "backend" / "alembic"
    assert selection.migrations_reason([".github/actions/backend-venv/action.yml"], graph, alembic)
    assert selection.migrations_reason([".github/actions/uv-sync/action.yml"], graph, alembic)


def test_migrations_run_for_what_the_job_s_own_migration_runner_imports(graph, tmp_path):
    # The job calls run_migrations() itself: what its functions import runs too.
    alembic = tmp_path / "backend" / "alembic"
    assert selection.migrations_reason(["backend/app/core/indexes.py"], graph, alembic)


def test_a_change_to_the_selection_runs_every_suite_it_decides(graph, frontend, tmp_path):
    # Otherwise a broken rule would decide whether the suite that catches it runs.
    change = ["backend/tests/selection.py"]
    assert selection.migrations_reason(change, graph, tmp_path / "backend" / "alembic")
    assert select_frontend_unit(change, frontend).mode == "full"
    assert selection.frontend_build_reason(change)
    # And the select job's own answer carries it: the graph is the small tree's, not the repository's.
    assert selection.run_ci(change, None, tmp_path / "out", graph=graph)["frontend-build"] == "true"


def test_a_file_of_tests_itself_selects_by_its_name_not_by_the_folder_every_test_imports(graph):
    # Every test writes "tests" (from tests.x import): the folder would select the whole suite.
    files = _files(["backend/tests/shard_durations.json"], graph)
    assert "tests/test_render.py" not in files and "tests/test_billing.py" not in files


def test_a_file_marked_integration_as_a_whole_takes_no_unit_shard(graph):
    marked = selection.Selection(reasons={"tests/test_db_postgres.py": "changed"})
    assert selection.plan_backend(marked, graph)["unit"] == []
    assert selection.plan_backend(marked, graph)["integration"] == ["1/1"]
    mixed = selection.Selection(reasons={"tests/test_mixed.py": "changed"})
    assert selection.plan_backend(mixed, graph)["unit"] == ["1/1"]


def test_the_browser_tool_takes_the_share_of_its_seconds_selected(graph, tmp_path):
    # Its files are dealt by seconds: one slow file selected takes the shards its seconds are worth.
    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / "unit.json").write_text("{}", encoding="utf-8")
    (weights / "chromium.json").write_text('{"tests/test_slow_chromium.py": 900}', encoding="utf-8")
    marked = "import pytest\npytestmark = pytest.mark.chromium\n"
    backend = _tree(
        tmp_path / "chromium-tree",
        {
            "app/__init__.py": "",
            "tests/__init__.py": "",
            "tests/test_slow_chromium.py": marked,
            **{f"tests/test_quick_{i}_chromium.py": marked for i in range(99)},
        },
    )
    tree = Graph.read(backend)
    slow = selection.Selection(reasons={"tests/test_slow_chromium.py": "changed"})
    quick = selection.Selection(reasons={"tests/test_quick_0_chromium.py": "changed"})
    # One file is one shard whatever it weighs; the whole suite takes five.
    assert selection.plan_backend(slow, tree, weights)["chromium"] == ["1/1"]
    assert selection.plan_backend(selection.Selection(full="all"), tree, weights)["chromium"] == shard_list(5)
    # Three files holding 900 s of the 999 take three shards; by file count (3 of 100) they took one.
    assert selection._shards(900, 999, "chromium", files=3) == 3
    assert selection._shards(3, 100, "chromium") == 1
    assert selection.plan_backend(quick, tree, weights)["chromium"] == ["1/1"]


def test_a_file_that_does_not_parse_runs_everything(tmp_path):
    backend = _tree(tmp_path / "backend", {"app/__init__.py": "", "app/broken.py": "def (:\n", "tests/__init__.py": ""})
    assert select_backend(["backend/app/__init__.py"], Graph.read(backend)).full


def test_path_pieces_are_whole_names():
    pieces = path_pieces('ROOT / "frontend" / "en.json"; frontend_dist = 1')
    assert {"frontend", "en.json", "en"} <= pieces
    assert "frontend_dist" in pieces and "dist" not in pieces


def test_the_selected_files_variable(tmp_path):
    assert read_selected_files(None) is None
    assert read_selected_files("") is None
    listed = tmp_path / "files.txt"
    listed.write_text("tests/test_a.py\n\ntests/test_b.py\n", encoding="utf-8")
    assert read_selected_files(str(listed)) == {"tests/test_a.py", "tests/test_b.py"}
    with pytest.raises(ValueError, match="not a file"):
        read_selected_files(str(tmp_path / "missing.txt"))


def test_shards_follow_the_share_of_the_suite_selected():
    assert shard_list(3) == ["1/3", "2/3", "3/3"]
    assert selection._shards(0, 400, "integration") == 0
    assert selection._shards(1, 400, "integration") == 1
    assert selection._shards(400, 400, "integration") == selection.MAX_SHARDS["integration"]
    assert selection._shards(2, 68, "chromium") == 1


@pytest.fixture
def frontend(tmp_path: Path) -> Path:
    return _tree(
        tmp_path / "frontend",
        {
            "src/lib/a.ts": "export const a = 1;\n",
            "src/lib/a.test.ts": "import { a } from './a';\n",
            "tests_unit/reads-backend.test.ts": (
                "import { readdirSync, readFileSync } from 'node:fs';\nconst files = readdirSync('../backend/app');\n"
            ),
            "src/i18n/plurals.test.ts": (
                "import { readdirSync } from 'node:fs';\nconst sources = readdirSync(join(cwd, 'src'));\n"
            ),
            "src/lib/own-source.test.ts": (
                "import { readFileSync } from 'node:fs';\nreadFileSync('src/pages/own-source.tsx'); // the backend reason\n"
            ),
            "tests_unit/reads-nothing.test.ts": "const p = '../backend/app';\n",
            "src/testing/forms.ts": "import { readFileSync } from 'node:fs';\nreadFileSync(join(dir, 'fixtures', 'forms.json'));\n",
            "src/lib/forms.test.ts": "import { FORMS } from '../testing/forms';\n",
            "vitest.setup.ts": "import { failOnWarnings } from './src/testing/warnings';\n",
            "src/testing/warnings.ts": "import { LIBRARY_DEFECTS } from './defects';\n",
            "src/testing/defects.ts": "export const LIBRARY_DEFECTS = [];\n",
        },
    )


def test_frontend_unit_relates_the_changed_sources_and_the_tests_that_list_their_area(frontend):
    # plurals lists src/ and reads every file of it; own-source reads one file, which names another.
    picked = select_frontend_unit(["frontend/src/lib/a.ts"], frontend)
    assert picked == FrontendUnit("related", picked.reason, ["src/i18n/plurals.test.ts", "src/lib/a.ts"])


def test_a_reader_that_names_an_area_without_listing_it_is_left_out(frontend):
    # own-source writes src/ and "backend" (in a comment) but reads one file of its own.
    assert "src/lib/own-source.test.ts" not in select_frontend_unit(["backend/app/x.py"], frontend).related


def test_frontend_unit_adds_the_tests_that_read_a_changed_area_from_disk(frontend):
    picked = select_frontend_unit(["backend/app/x.py"], frontend)
    assert picked.related == ["tests_unit/reads-backend.test.ts"]


def test_frontend_unit_follows_a_helper_that_reads_the_changed_file(frontend):
    # The helper reads it for its tests: vitest related runs the tests that import the helper.
    picked = select_frontend_unit(["backend/tests/fixtures/forms.json"], frontend)
    assert "src/testing/forms.ts" in picked.related
    assert "src/lib/forms.test.ts" not in picked.related


def test_frontend_unit_skips_a_change_it_does_not_reach(frontend):
    assert select_frontend_unit(["docs/docs/index.md"], frontend).mode == "skip"


@pytest.mark.parametrize("path", ["frontend/package.json", "frontend/vitest.config.ts", "frontend/tests_unit/setup.ts"])
def test_frontend_unit_runs_whole_on_what_every_test_runs_under(frontend, path):
    assert select_frontend_unit([path], frontend).mode == "full"


@pytest.mark.parametrize("path", ["frontend/src/testing/warnings.ts", "frontend/src/testing/defects.ts"])
def test_frontend_unit_runs_whole_on_what_a_setup_file_imports_at_any_depth(frontend, path):
    # vitest related follows the spec files' imports, not the setup files'.
    assert select_frontend_unit([path], frontend).mode == "full"


def test_e2e_skips_a_change_the_stack_does_not_carry(graph):
    assert not any(e2e_projects(["docs/docs/index.md", "backend/tests/test_billing.py", "README.md"], graph).values())


def test_e2e_runs_for_the_markdown_the_boot_seeds_and_not_for_a_package_s_documentation(graph):
    # The image carries backend/app: a module reads persona/one.md, none reads AGENTS.md.
    assert e2e_projects(["backend/app/data/persona/one.md"], graph)["core"]
    assert not any(e2e_projects(["backend/app/services/AGENTS.md"], graph).values())


def test_e2e_runs_core_alone_for_a_change_outside_the_areas(graph):
    projects = e2e_projects(["backend/app/services/billing.py"], graph)
    assert projects["core"] and not projects["a2a"] and not projects["evaluation"]


@pytest.mark.parametrize(
    "path", ["Dockerfile", "frontend/yarn.lock", "backend/alembic/versions/x.py", "backend/start-uvicorn.sh"]
)
def test_e2e_runs_every_project_for_what_the_stack_is_built_from(graph, path):
    assert all(e2e_projects([path], graph).values())


def test_a_core_spec_runs_core_and_the_harness_every_project(graph):
    projects = e2e_projects(["frontend/tests_e2e/navigation.spec.ts"], graph)
    assert projects["core"] and not projects["a2a"] and not projects["evaluation"]
    for harness in ("frontend/tests_e2e/helpers.ts", "frontend/tests_e2e/model/chat.page.ts"):
        assert all(e2e_projects([harness], graph).values()), harness


def test_e2e_runs_an_area_for_its_specs_and_its_ui(graph):
    assert e2e_projects(["frontend/tests_e2e/a2a/05-handover.spec.ts"], graph)["a2a"]
    assert e2e_projects(["frontend/src/pages/evaluations/List.tsx"], graph)["evaluation"]


# The real tree, read once: what CI's select job reads.

_PERSONA = "backend/app/agents/seed_data/agent_persona/aev-detection-rules-generator.md"


@pytest.fixture(scope="module")
def repository() -> Graph:
    return Graph.read()


def test_the_whole_backend_parses_under_the_suite_interpreter(repository):
    # A file the graph cannot parse turns every merge group into a full run.
    assert repository.unparsed == []


def test_the_tests_a_queue_run_failed_on_are_selected_by_its_change(repository):
    # A merge group of 13 Nov 2026: a settings change broke five request-field
    # tripwires, the queue's only failures that week.
    changed = ["backend/app/api/deps.py", "backend/app/api/v1/settings.py", "backend/app/core/config.py"]
    files = set(select_backend(changed, repository).files)
    assert {"tests/test_assignment_text_length.py", "tests/test_webhook_secret_enforcement.py"} <= files


def test_the_queue_failures_of_18_nov_2026_are_selected_by_their_change(repository):
    # An httpx client built in a new a2a module: the egress tripwire walks app/ (APP.rglob).
    changed = ["backend/app/a2a/client/probe.py", "backend/app/a2a/client/credentials.py"]
    assert "tests/test_egress_funnel.py" in select_backend(changed, repository).files


def test_a_video_studio_change_leaves_most_of_the_suite_out(repository):
    picked = select_backend(["backend/app/services/video_studio/encoder.py"], repository)
    assert picked.full is None
    assert 0 < len(picked.files) < len(repository.test_files) / 2


def test_a_package_s_own_documentation_reaches_almost_nothing(repository):
    picked = select_backend(["backend/app/agents/AGENTS.md"], repository)
    assert picked.full is None
    assert len(picked.files) < len(repository.test_files) / 20


def test_the_migration_helpers_run_the_migrations_job_and_most_modules_do_not(repository):
    chain = selection.migration_chain_modules(repository)
    assert {"app.core.migration_ddl", "app.core.config", "app.core.database", "app.models"} <= chain
    assert "app.core.url_identity" in chain  # a validator of Settings(), built as env.py loads config
    assert len(chain) < len([name for name in repository.modules if name.startswith("app")]) / 3
    assert selection.migrations_reason(["backend/app/core/migration_ddl.py"], repository)
    assert selection.migrations_reason(["backend/app/services/video_studio/encoder.py"], repository) is None


def test_a_seeded_persona_runs_the_e2e_stack_and_a_package_s_agents_md_does_not(repository):
    persona = "backend/app/agents/seed_data/agent_persona/aev-detection-rules-generator.md"
    assert e2e_projects([persona], repository)["core"]
    assert not any(e2e_projects(["backend/app/agents/AGENTS.md"], repository).values())


def test_the_dashboard_forms_fixture_reaches_the_helper_its_tests_read_it_through():
    picked = select_frontend_unit(["backend/tests/fixtures/dashboards/platform_forms.json"])
    assert "src/testing/platform-forms.ts" in picked.related


@pytest.mark.parametrize(
    ("changed", "guard"),
    [
        # A tripwire that reads the module's source by its path.
        ("backend/app/api/v1/agent_import.py", "tests/test_agent_copy_manifest.py"),
        ("backend/app/api/v1/chat.py", "tests/test_handover_gating.py"),
        # Tests that glob the seeded personas.
        (_PERSONA, "tests/test_seeded_loop_caps.py"),
        (_PERSONA, "tests/test_agent_facing_placeholders.py"),
        # A snapshot reached through the script that renders it.
        ("backend/app/services/hitl_whitelist_derived.py", "tests/test_hitl_whitelist_snapshots.py"),
        # A walk of Path(app.__file__).
        ("backend/app/agents/seed.py", "tests/test_lone_surrogate_writes.py"),
        # Tests that walk the test files.
        ("backend/tests/test_evaluations.py", "tests/test_evaluation_guards.py"),
        ("backend/tests/test_evaluations.py", "tests/test_malformed_path_id_routes.py"),
    ],
)
def test_a_tripwire_runs_when_what_it_guards_changes(repository, changed, guard):
    # Each pair a review of the selection found missing (#162).
    assert guard in select_backend([changed], repository).files


@pytest.mark.parametrize("helper", ["frozen_clock.py", "redis_unreachable.py", "stall_guard.py"])
def test_a_helper_conftest_applies_to_every_test_runs_the_suite(repository, helper):
    assert select_backend([f"backend/tests/{helper}"], repository).full


def test_the_design_system_warnings_run_every_vitest_file():
    assert select_frontend_unit(["frontend/src/testing/design-system-warnings.ts"]).mode == "full"


def test_the_index_maintenance_the_migration_runner_calls_runs_the_migrations_job(repository):
    assert selection.migrations_reason(["backend/app/core/index_maintenance.py"], repository)


def test_every_e2e_project_the_workflow_shards_is_one_the_selection_decides():
    # A project missing from E2E_AREAS would never run in the queue, and nothing would fail.
    workflow = yaml.safe_load((selection.REPO_ROOT / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["select"]["steps"]
    matrix = next(step["env"]["MATRIX"] for step in steps if "MATRIX" in step.get("env", {}))
    projects = {entry["project"] for entry in json.loads(matrix)}
    assert projects == {"core", *selection.E2E_AREAS}
    config = (selection.REPO_ROOT / "frontend" / "playwright.config.ts").read_text(encoding="utf-8")
    assert projects <= set(re.findall(r"name:\s*['\"]([\w-]+)['\"]", config))


def test_any_change_runs_the_tests_that_read_every_tracked_file(repository):
    # The image carries docs/ as text the boot reads as UTF-8: the hygiene tripwire checks it.
    assert "tests/test_repo_text_hygiene.py" in select_backend(["docs/docs/index.md"], repository).files
