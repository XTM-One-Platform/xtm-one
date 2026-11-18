"""The merge queue's test selection (``tests/selection.py``): each rule on a small tree, then the real one."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

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


def test_a_changed_test_file_runs(graph):
    assert _files(["backend/tests/test_billing.py"], graph) == {"tests/test_billing.py"}


def test_a_tree_reader_runs_on_any_app_module_and_only_then(graph):
    # Through app_sources, or a walk of the directory it names "app" (a tripwire's APP.rglob).
    assert {"tests/test_tree.py", "tests/test_walker.py"} <= _files(["backend/app/services/seeds.py"], graph)
    assert "tests/test_tree.py" not in _files(["frontend/src/App.tsx"], graph)


def test_a_file_outside_backend_selects_the_tests_that_name_its_area(graph):
    assert _files(["frontend/src/App.tsx"], graph) == {"tests/test_frontend_catalog.py"}
    assert _files(["docs/docs/index.md"], graph) == set()


def test_a_data_file_reaches_the_tests_of_the_module_that_names_its_folder(graph):
    assert "tests/test_seeds.py" in _files(["backend/app/data/persona/one.md"], graph)


def test_a_data_file_no_module_names_runs_everything(graph):
    assert select_backend(["backend/app/data/orphan/two.md"], graph).full


def test_a_markdown_file_beside_a_package_s_modules_is_its_documentation(graph):
    # No module names it: it reaches no test, rather than every module naming its directory.
    assert _files(["backend/app/services/AGENTS.md"], graph) == set()


def test_a_deleted_module_reaches_what_still_names_it_and_no_more(graph):
    files = _files(["backend/app/services/gone.py", "backend/tests/test_removed.py"], graph)
    # The tree readers run too: a deletion changes the tree they read.
    assert files == {"tests/test_gone.py", "tests/test_tree.py", "tests/test_walker.py"}


@pytest.mark.parametrize(
    "path",
    ["backend/tests/conftest.py", "backend/uv.lock", ".github/workflows/ci.yml", "backend/tests/selection.py"],
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
    assert len(chain) < len([name for name in repository.modules if name.startswith("app")]) / 5
    assert selection.migrations_reason(["backend/app/core/migration_ddl.py"], repository)
    assert selection.migrations_reason(["backend/app/services/video_studio/encoder.py"], repository) is None


def test_a_seeded_persona_runs_the_e2e_stack_and_a_package_s_agents_md_does_not(repository):
    persona = "backend/app/agents/seed_data/agent_persona/aev-detection-rules-generator.md"
    assert e2e_projects([persona], repository)["core"]
    assert not any(e2e_projects(["backend/app/agents/AGENTS.md"], repository).values())


def test_the_dashboard_forms_fixture_reaches_the_helper_its_tests_read_it_through():
    picked = select_frontend_unit(["backend/tests/fixtures/dashboards/platform_forms.json"])
    assert "src/testing/platform-forms.ts" in picked.related


def test_any_change_runs_the_tests_that_read_every_tracked_file(repository):
    # The image carries docs/ as text the boot reads as UTF-8: the hygiene tripwire checks it.
    assert "tests/test_repo_text_hygiene.py" in select_backend(["docs/docs/index.md"], repository).files
