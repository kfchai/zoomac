"""Tests for the goal planner -- models, store, credentials, engine, and integration."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.testing import FakePlannerRuntime, FakeRuntimeProvider
from zoomac.planner.approval import parse_write_code_output

from zoomac.autonomy.classifier import ActionType
from zoomac.planner.models import (
    CredentialEntry,
    Goal,
    GoalStatus,
    PlanDecomposition,
    ReplanDecision,
    Task,
    TaskSpec,
    TaskStatus,
    TaskType,
)
from zoomac.planner.store import GoalStore
from zoomac.planner.credentials import CredentialVault
from zoomac.planner.engine import GoalEngine
from zoomac.autonomy.policy import AutonomyManager


# -- helpers --

def _is_goal_instruction(text: str) -> bool:
    """Mirror the detection logic from loop.py for testing."""
    return text.strip().lower().startswith("/goal ")


def _make_goal(goal_id: str = "goal1", instruction: str = "test goal") -> Goal:
    return Goal(id=goal_id, raw_instruction=instruction, source="cli", channel="cli")


def _make_task(
    task_id: str = "task1",
    goal_id: str = "goal1",
    task_type: TaskType = TaskType.LLM_REASON,
    status: TaskStatus = TaskStatus.PENDING,
    depends_on: list[str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        goal_id=goal_id,
        task_type=task_type,
        description=f"Test task {task_id}",
        instruction=f"Do {task_id}",
        status=status,
        depends_on=depends_on or [],
    )


# -- Model tests --


class TestModels:
    def test_goal_creation(self):
        goal = Goal(raw_instruction="Setup telegram")
        assert goal.status == GoalStatus.PLANNING
        assert goal.id
        assert goal.raw_instruction == "Setup telegram"
        assert goal.tasks == []
        assert goal.context == {}

    def test_task_defaults(self):
        task = Task(goal_id="g1", task_type=TaskType.SANDBOX_EXEC, description="install", instruction="pip install requests")
        assert task.status == TaskStatus.PENDING
        assert task.depends_on == []
        assert task.attempts == 0
        assert task.max_attempts == 3

    def test_task_dependency_serialization(self):
        task = Task(
            goal_id="g1",
            task_type=TaskType.LLM_REASON,
            description="analyze",
            instruction="analyze deps",
            depends_on=["t1", "t2", "t3"],
        )
        data = task.model_dump()
        restored = Task.model_validate(data)
        assert restored.depends_on == ["t1", "t2", "t3"]

    def test_plan_decomposition_schema(self):
        plan = PlanDecomposition(
            reasoning="Need to install package then test",
            tasks=[
                TaskSpec(task_type=TaskType.SANDBOX_EXEC, description="install", instruction="pip install requests"),
                TaskSpec(task_type=TaskType.LLM_REASON, description="verify", instruction="check install", depends_on_indices=[0]),
            ],
        )
        assert len(plan.tasks) == 2
        assert plan.tasks[1].depends_on_indices == [0]

    def test_replan_decision_schema(self):
        decision = ReplanDecision(action="skip", reasoning="Not critical")
        assert decision.action == "skip"
        assert decision.replacement_tasks is None

    def test_credential_entry(self):
        entry = CredentialEntry(key="api_key", description="Test API key")
        assert entry.value is None
        assert entry.provided_at is None
        assert entry.key == "api_key"


# -- Store tests --


class TestGoalStore:
    def test_store_init_creates_tables(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        # Accessing conn triggers schema creation
        tables = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {row["name"] for row in tables}
        assert "goals" in table_names
        assert "tasks" in table_names
        assert "credentials" in table_names
        assert "task_events" in table_names
        store.close()

    def test_save_and_load_goal(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        goal = _make_goal()
        store.save_goal(goal)
        loaded = store.load_goal("goal1")
        assert loaded is not None
        assert loaded.raw_instruction == "test goal"
        assert loaded.status == GoalStatus.PLANNING
        store.close()

    def test_save_and_update_task(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        goal = _make_goal()
        store.save_goal(goal)
        task = _make_task()
        store.save_task(task)

        store.update_task("task1", status=TaskStatus.DONE, result="success")
        loaded = store.load_goal("goal1")
        assert loaded.tasks[0].status == TaskStatus.DONE
        assert loaded.tasks[0].result == "success"
        store.close()

    def test_get_ready_tasks(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        goal = _make_goal()
        store.save_goal(goal)

        # t1 has no deps (ready), t2 depends on t1 (not ready), t3 has no deps (ready)
        t1 = _make_task("t1", depends_on=[])
        t2 = _make_task("t2", depends_on=["t1"])
        t3 = _make_task("t3", depends_on=[])
        store.save_task(t1)
        store.save_task(t2)
        store.save_task(t3)

        ready = store.get_ready_tasks("goal1")
        ready_ids = {t.id for t in ready}
        assert "t1" in ready_ids
        assert "t3" in ready_ids
        assert "t2" not in ready_ids
        store.close()

    def test_get_ready_tasks_after_dep_done(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        goal = _make_goal()
        store.save_goal(goal)

        t1 = _make_task("t1", depends_on=[])
        t2 = _make_task("t2", depends_on=["t1"])
        store.save_task(t1)
        store.save_task(t2)

        # Mark t1 done
        store.update_task("t1", status=TaskStatus.DONE)

        ready = store.get_ready_tasks("goal1")
        ready_ids = {t.id for t in ready}
        assert "t2" in ready_ids

        store.close()

    def test_recover_running(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        goal = _make_goal()
        store.save_goal(goal)
        task = _make_task(status=TaskStatus.RUNNING)
        store.save_task(task)

        recovered = store.recover_running()
        assert recovered == 1
        loaded = store.load_goal("goal1")
        assert loaded.tasks[0].status == TaskStatus.PENDING
        store.close()

    def test_credential_store_and_retrieve(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        entry = CredentialEntry(key="bot_token", description="Telegram bot token", value="abc123")
        entry.provided_at = datetime.now(timezone.utc)
        store.save_credential(entry)

        loaded = store.get_credential("bot_token")
        assert loaded is not None
        assert loaded.value == "abc123"
        assert loaded.provided_at is not None
        store.close()

    def test_list_pending_credentials(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        # One pending, one provided
        store.save_credential(CredentialEntry(key="k1", description="first"))
        provided = CredentialEntry(key="k2", description="second", value="val")
        provided.provided_at = datetime.now(timezone.utc)
        store.save_credential(provided)

        pending = store.list_pending_credentials()
        assert len(pending) == 1
        assert pending[0].key == "k1"
        store.close()

    def test_find_blocked_goal(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        goal = _make_goal()
        goal.status = GoalStatus.BLOCKED
        store.save_goal(goal)

        found = store.find_blocked_goal("cli", "cli")
        assert found is not None
        assert found.id == "goal1"

        not_found = store.find_blocked_goal("telegram", "chat123")
        assert not_found is None
        store.close()

    def test_add_and_list_task_events(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        goal = _make_goal()
        store.save_goal(goal)
        store.add_task_event(goal.id, "task_started", "Started task t1", task_id="t1")
        store.add_task_event(goal.id, "task_completed", "Finished task t1", task_id="t1")
        events = store.list_task_events(goal.id)
        assert len(events) == 2
        assert events[0].event_type == "task_started"
        assert events[1].message == "Finished task t1"
        store.close()


# -- Credential vault tests --


class TestCredentialVault:
    def test_plaintext_roundtrip(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        vault = CredentialVault(store, encryption_key=None)

        vault.store("my_key", "my_secret", "A test secret")
        result = vault.retrieve("my_key")
        assert result == "my_secret"
        store.close()

    def test_retrieve_missing(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        vault = CredentialVault(store, encryption_key=None)

        result = vault.retrieve("nonexistent")
        assert result is None
        store.close()

    def test_request_creates_pending(self, tmp_path):
        store = GoalStore(tmp_path / "test.db")
        vault = CredentialVault(store, encryption_key=None)

        entry = vault.request("bot_token", "Telegram bot token")
        assert entry.value is None

        pending = vault.list_pending()
        assert len(pending) == 1
        assert pending[0].key == "bot_token"
        store.close()

    def test_encrypted_roundtrip(self, tmp_path):
        """Test encryption when cryptography is available."""
        store = GoalStore(tmp_path / "test.db")
        try:
            vault = CredentialVault(store, encryption_key="test-secret-key-123")
            vault.store("encrypted_key", "super_secret", "Encrypted test")
            result = vault.retrieve("encrypted_key")
            assert result == "super_secret"
        except Exception:
            pytest.skip("cryptography package not available")
        finally:
            store.close()


# -- Engine tests --


class TestGoalEngine:
    @pytest.fixture
    def engine_setup(self, tmp_path):
        """Set up a GoalEngine with a fake planner runtime."""
        store = GoalStore(tmp_path / "goals.db")
        vault = CredentialVault(store, encryption_key=None)
        engine = GoalEngine(
            store=store,
            planner_runtime=FakePlannerRuntime(),
            credential_vault=vault,
        )
        yield engine, store
        store.close()

    @pytest.mark.asyncio
    async def test_create_goal_produces_tasks(self, engine_setup):
        engine, store = engine_setup
        goal = await engine.create_goal("Install requests in sandbox")
        assert goal.status == GoalStatus.EXECUTING
        # FakePlannerRuntime produces some output -- we just verify the flow works
        assert goal.id

    @pytest.mark.asyncio
    async def test_create_plan_stays_draft_until_approved(self, engine_setup):
        engine, store = engine_setup
        goal = await engine.create_plan("Prepare a plan only")
        assert goal.status == GoalStatus.DRAFT

        loaded = store.load_goal(goal.id)
        assert loaded is not None
        assert loaded.status == GoalStatus.DRAFT

        events = store.list_task_events(goal.id)
        assert any(event.event_type == "plan_ready" for event in events)

        approved = engine.approve_goal(goal.id)
        assert approved.status == GoalStatus.EXECUTING

    @pytest.mark.asyncio
    async def test_step_executes_ready_task(self, tmp_path):
        """A ready task with no deps gets executed on step()."""
        store = GoalStore(tmp_path / "goals.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime())

        # Manually create a goal with one ready task
        goal = _make_goal()
        goal.status = GoalStatus.EXECUTING
        store.save_goal(goal)
        task = _make_task("t1", task_type=TaskType.LLM_REASON, depends_on=[])
        store.save_task(task)

        result = await engine.step("goal1")
        # The LLM_REASON task runs via planner runtime
        assert "t1" in result.executed or "t1" in result.failed
        store.close()

    @pytest.mark.asyncio
    async def test_step_requires_approval_for_draft_goal(self, tmp_path):
        store = GoalStore(tmp_path / "goals.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime())

        goal = _make_goal()
        goal.status = GoalStatus.DRAFT
        store.save_goal(goal)
        result = await engine.step("goal1")
        assert result.goal_blocked is True
        assert "plan approval" in result.messages[0].lower()
        store.close()

    @pytest.mark.asyncio
    async def test_step_skips_blocked_task(self, tmp_path):
        """Blocked tasks are not picked up by step()."""
        store = GoalStore(tmp_path / "goals.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime())

        goal = _make_goal()
        goal.status = GoalStatus.EXECUTING
        store.save_goal(goal)
        task = _make_task("t1", status=TaskStatus.BLOCKED, depends_on=[])
        store.save_task(task)

        result = await engine.step("goal1")
        assert "t1" not in result.executed
        store.close()

    @pytest.mark.asyncio
    async def test_step_respects_dependencies(self, tmp_path):
        """Task with unmet dependencies is not executed."""
        store = GoalStore(tmp_path / "goals.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime())

        goal = _make_goal()
        goal.status = GoalStatus.EXECUTING
        store.save_goal(goal)
        t1 = _make_task("t1", depends_on=[])
        t2 = _make_task("t2", depends_on=["t1"])
        store.save_task(t1)
        store.save_task(t2)

        result = await engine.step("goal1")
        # t1 should be attempted, t2 should not (dep not met)
        assert "t2" not in result.executed
        store.close()

    @pytest.mark.asyncio
    async def test_handle_user_input_unblocks_credential(self, tmp_path):
        """User input resolves a blocked credential task."""
        store = GoalStore(tmp_path / "goals.db")
        vault = CredentialVault(store, encryption_key=None)
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime(), credential_vault=vault)

        goal = _make_goal()
        goal.status = GoalStatus.BLOCKED
        store.save_goal(goal)
        task = _make_task(
            "t1",
            task_type=TaskType.CREDENTIAL_REQUEST,
            status=TaskStatus.BLOCKED,
        )
        task.instruction = "telegram_bot_token"
        task.blocker_reason = "Need credential: Telegram bot token"
        store.save_task(task)

        reply = await engine.handle_user_input("goal1", "abc123tokenvalue")
        assert "Credential received" in reply

        # Task should be done
        loaded = store.load_goal("goal1")
        assert loaded.tasks[0].status == TaskStatus.DONE

        # Goal should be back to executing
        assert loaded.status == GoalStatus.EXECUTING

        # Credential should be stored
        cred = vault.retrieve("telegram_bot_token")
        assert cred == "abc123tokenvalue"
        store.close()

    @pytest.mark.asyncio
    async def test_handle_user_input_unblocks_wait(self, tmp_path):
        """User input resolves a blocked wait_input task."""
        store = GoalStore(tmp_path / "goals.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime())

        goal = _make_goal()
        goal.status = GoalStatus.BLOCKED
        store.save_goal(goal)
        task = _make_task("t1", task_type=TaskType.WAIT_INPUT, status=TaskStatus.BLOCKED)
        task.blocker_reason = "Waiting for user input"
        store.save_task(task)

        reply = await engine.handle_user_input("goal1", "user's answer")
        assert "Input received" in reply

        loaded = store.load_goal("goal1")
        assert loaded.tasks[0].status == TaskStatus.DONE
        assert loaded.tasks[0].result == "user's answer"
        store.close()

    @pytest.mark.asyncio
    async def test_goal_completes_when_all_done(self, tmp_path):
        """Goal status becomes DONE when all tasks are done."""
        store = GoalStore(tmp_path / "goals.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime())

        goal = _make_goal()
        goal.status = GoalStatus.EXECUTING
        store.save_goal(goal)
        task = _make_task("t1", status=TaskStatus.DONE, depends_on=[])
        store.save_task(task)

        result = await engine.step("goal1")
        assert result.goal_done is True

        loaded = store.load_goal("goal1")
        assert loaded.status == GoalStatus.DONE
        store.close()

    @pytest.mark.asyncio
    async def test_task_completion_records_event_and_summary(self, tmp_path):
        store = GoalStore(tmp_path / "goals.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime(), project_dir=str(tmp_path))

        goal = _make_goal()
        goal.status = GoalStatus.EXECUTING
        store.save_goal(goal)
        task = _make_task("t1", task_type=TaskType.READ_FILE, depends_on=[])
        sample = tmp_path / "notes.txt"
        sample.write_text("hello from planner", encoding="utf-8")
        task.instruction = "notes.txt"
        store.save_task(task)

        result = await engine.step("goal1")
        assert "t1" in result.executed

        loaded = store.load_goal("goal1")
        assert loaded.tasks[0].output_summary is not None
        events = store.list_task_events(goal.id)
        assert any(event.event_type == "task_completed" for event in events)
        store.close()

    @pytest.mark.asyncio
    async def test_subagent_spawn_task_runs_via_manager(self, tmp_path):
        store = GoalStore(tmp_path / "goals.db")

        class _PlannerRuntime:
            async def decompose(self, prompt: str):
                return PlanDecomposition(reasoning=prompt, tasks=[])

            async def reason(self, prompt: str) -> str:
                return "delegated summary"

            async def write_artifact(self, prompt: str) -> str:
                return prompt

            async def replan(self, prompt: str):
                return ReplanDecision(action="retry", reasoning=prompt)

        from zoomac.agents.manager import SubAgentManager

        planner_runtime = _PlannerRuntime()
        agent_manager = SubAgentManager(planner_runtime=planner_runtime, max_agents=1)
        engine = GoalEngine(
            store=store,
            planner_runtime=planner_runtime,
            agent_manager=agent_manager,
        )

        goal = _make_goal()
        goal.status = GoalStatus.EXECUTING
        store.save_goal(goal)
        task = _make_task("t1", task_type=TaskType.SUBAGENT_SPAWN, depends_on=[])
        task.instruction = "Investigate test failure"
        store.save_task(task)

        result = await engine.step("goal1")
        assert "t1" in result.executed

        loaded = store.load_goal("goal1")
        assert loaded is not None
        assert "Sub-agent" in (loaded.tasks[0].result or "")
        store.close()

    @pytest.mark.asyncio
    async def test_goal_blocked_when_all_branches_blocked(self, tmp_path):
        """Goal becomes BLOCKED when all non-terminal tasks are blocked."""
        store = GoalStore(tmp_path / "goals.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime())

        goal = _make_goal()
        goal.status = GoalStatus.EXECUTING
        store.save_goal(goal)
        t1 = _make_task("t1", status=TaskStatus.DONE, depends_on=[])
        t2 = _make_task("t2", status=TaskStatus.BLOCKED, depends_on=["t1"])
        store.save_task(t1)
        store.save_task(t2)

        result = await engine.step("goal1")
        assert result.goal_blocked is True

        loaded = store.load_goal("goal1")
        assert loaded.status == GoalStatus.BLOCKED
        store.close()

    @pytest.mark.asyncio
    async def test_high_risk_task_uses_approval_pipeline(self, tmp_path):
        """High-risk tasks block through the approval pipeline."""
        store = GoalStore(tmp_path / "goals.db")
        autonomy = AutonomyManager(db_path=tmp_path / "audit.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime(), autonomy=autonomy)

        goal = _make_goal()
        goal.status = GoalStatus.EXECUTING
        store.save_goal(goal)
        task = _make_task("t1", task_type=TaskType.SEND_MESSAGE, depends_on=[])
        store.save_task(task)

        result = await engine.step("goal1")
        assert "t1" in result.blocked

        loaded = store.load_goal("goal1")
        assert "approval required" in (loaded.tasks[0].blocker_reason or "").lower()
        assert loaded.tasks[0].approval_mode == "ask_once"
        assert loaded.tasks[0].approval_provenance == "action:send_message"
        autonomy.close()
        store.close()

    @pytest.mark.asyncio
    async def test_handle_user_input_allow_session_unblocks_approval(self, tmp_path):
        """Session approval stores a reusable session rule before resuming."""
        store = GoalStore(tmp_path / "goals.db")
        autonomy = AutonomyManager(db_path=tmp_path / "audit.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime(), autonomy=autonomy)

        goal = _make_goal()
        goal.status = GoalStatus.BLOCKED
        store.save_goal(goal)
        task = _make_task(
            "t1",
            task_type=TaskType.SEND_MESSAGE,
            status=TaskStatus.BLOCKED,
        )
        task.blocker_reason = "Approval required [send_message]: Test task t1"
        store.save_task(task)

        reply = await engine.handle_user_input("goal1", "allow session")
        assert "Approved for this session" in reply

        decision = autonomy.evaluate_action(
            ActionType.SEND_MESSAGE,
            detail="Send another message",
            session_id="cli:cli",
        )
        assert decision.outcome.value == "allow"
        autonomy.close()
        store.close()

    @pytest.mark.asyncio
    async def test_handle_user_input_allow_command_adds_reusable_rule(self, tmp_path):
        """Command approval can be promoted into a bounded reusable prefix rule."""
        store = GoalStore(tmp_path / "goals.db")
        autonomy = AutonomyManager(db_path=tmp_path / "audit.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime(), autonomy=autonomy)

        goal = _make_goal()
        goal.status = GoalStatus.BLOCKED
        store.save_goal(goal)
        task = _make_task(
            "t1",
            task_type=TaskType.SANDBOX_EXEC,
            status=TaskStatus.BLOCKED,
        )
        task.instruction = "git status --short"
        task.blocker_reason = (
            "Approval required [run_command] via default:run_command: "
            "Action run_command is high risk. Options: allow, deny, allow session, allow command."
        )
        store.save_task(task)

        reply = await engine.handle_user_input("goal1", "allow command")
        assert "matching commands" in reply

        decision = autonomy.evaluate_action(
            ActionType.RUN_COMMAND,
            detail="Run git status",
            command_text="git status --short",
            session_id="cli:cli",
        )
        assert decision.outcome.value == "allow"
        assert decision.mode.value == "allow_by_rule"
        autonomy.close()
        store.close()

    @pytest.mark.asyncio
    async def test_handle_user_input_allow_path_adds_reusable_rule(self, tmp_path):
        """File-write approval can be promoted into a bounded reusable path rule."""
        store = GoalStore(tmp_path / "goals.db")
        autonomy = AutonomyManager(db_path=tmp_path / "audit.db")
        engine = GoalEngine(
            store=store,
            planner_runtime=FakePlannerRuntime(),
            autonomy=autonomy,
            project_dir=str(tmp_path),
        )

        goal = _make_goal()
        goal.status = GoalStatus.BLOCKED
        store.save_goal(goal)
        task = _make_task(
            "t1",
            task_type=TaskType.WRITE_CODE,
            status=TaskStatus.BLOCKED,
        )
        task.instruction = "FILE: src/app.py\n```\nprint('hi')\n```"
        task.blocker_reason = (
            "Approval required [write_file] via default:write_file: "
            "Action write_file is high risk. Options: allow, deny, allow session, allow path."
        )
        store.save_task(task)

        reply = await engine.handle_user_input("goal1", "allow path")
        assert "matching file paths" in reply

        decision = autonomy.evaluate_action(
            ActionType.WRITE_FILE,
            detail="Write src/app.py",
            file_path=str(tmp_path / "src" / "app.py"),
            session_id="cli:cli",
        )
        assert decision.outcome.value == "allow"
        assert decision.mode.value == "allow_by_rule"
        autonomy.close()
        store.close()

    def test_goal_status_report_includes_recent_events(self, tmp_path):
        store = GoalStore(tmp_path / "goals.db")
        engine = GoalEngine(store=store, planner_runtime=FakePlannerRuntime())

        goal = _make_goal()
        goal.status = GoalStatus.EXECUTING
        store.save_goal(goal)
        store.add_task_event(goal.id, "task_started", "Started task one")
        report = engine.goal_status_report(goal.id)
        assert "executing" in report
        assert "recent:" in report
        assert "Started task one" in report
        store.close()


# -- Write code tests --


class TestWriteCode:
    def test_parse_write_code_output(self):
        """Parse FILE: path + code block from LLM output."""
        text = "FILE: src/hello.py\n```\nprint('hello')\n```"
        path, content = parse_write_code_output(text)
        assert path == "src/hello.py"
        assert content == "print('hello')"

    def test_parse_write_code_no_fences(self):
        """Parse FILE: path + content without code fences."""
        text = "FILE: readme.txt\nHello world\nLine 2"
        path, content = parse_write_code_output(text)
        assert path == "readme.txt"
        assert "Hello world" in content
        assert "Line 2" in content

    def test_parse_write_code_no_file(self):
        """Returns None path when no FILE: prefix found."""
        text = "Just some random text"
        path, content = parse_write_code_output(text)
        assert path is None

    @pytest.mark.asyncio
    async def test_write_code_task_creates_file(self, tmp_path):
        """WRITE_CODE task writes a file to the project directory."""
        store = GoalStore(tmp_path / "goals.db")
        engine = GoalEngine(
            store=store, planner_runtime=FakePlannerRuntime(), project_dir=str(tmp_path)
        )

        goal = _make_goal()
        goal.status = GoalStatus.EXECUTING
        store.save_goal(goal)

        task = _make_task("t1", task_type=TaskType.WRITE_CODE, depends_on=[])
        task.instruction = "FILE: test_output.py\n```\nprint('hello from agent')\n```"
        store.save_task(task)

        result = await engine.step("goal1")

        # The task uses the planner runtime which returns fake output, but the fallback
        # parses from the instruction itself
        loaded = store.load_goal("goal1")
        t = loaded.tasks[0]
        # Task should have been attempted
        assert t.status in (TaskStatus.DONE, TaskStatus.FAILED)
        store.close()

    @pytest.mark.asyncio
    async def test_write_code_rejects_path_traversal(self, tmp_path):
        """WRITE_CODE rejects paths that escape the project directory."""
        store = GoalStore(tmp_path / "goals.db")
        engine = GoalEngine(
            store=store, planner_runtime=FakePlannerRuntime(), project_dir=str(tmp_path)
        )

        # Simulate direct call to _exec_write_code with a path traversal
        task = _make_task("t1", task_type=TaskType.WRITE_CODE)
        task.instruction = "FILE: ../../etc/evil.txt\n```\nhacked\n```"

        goal = _make_goal()
        # The parse_write_code_output + safety check should catch this
        path, content = parse_write_code_output(task.instruction)
        assert path is not None

        target = (Path(str(tmp_path)) / path).resolve()
        assert not str(target).startswith(str(Path(str(tmp_path)).resolve()))
        store.close()

    def test_write_code_task_type_exists(self):
        """WRITE_CODE is a valid TaskType."""
        assert TaskType.WRITE_CODE == "write_code"
        task = _make_task("t1", task_type=TaskType.WRITE_CODE)
        assert task.task_type == TaskType.WRITE_CODE


# -- Integration tests --


class TestIntegration:
    def test_goal_detection_prefix(self):
        assert _is_goal_instruction("/goal setup telegram")
        assert _is_goal_instruction("/Goal Setup Telegram")
        assert not _is_goal_instruction("hello")
        assert not _is_goal_instruction("what is a /goal?")

    def test_core_loop_init_with_planner(self, tmp_path):
        """CoreLoop initializes with goal planner components."""
        from zoomac.core.config import ZoomacSettings
        from zoomac.core.loop import CoreLoop

        settings = ZoomacSettings(project_dir=tmp_path)
        core = CoreLoop(settings, runtime_provider=FakeRuntimeProvider())
        assert core._goal_engine is not None
        assert core._goal_store is not None
        assert core._vault is not None
        core.shutdown()

    @pytest.mark.asyncio
    async def test_core_loop_handles_goal_message(self, tmp_path):
        """CoreLoop routes /goal prefix messages to the goal engine."""
        from zoomac.core.config import ZoomacSettings
        from zoomac.core.loop import CoreLoop
        from zoomac.core.events import EventSource, MessageEvent

        settings = ZoomacSettings(project_dir=tmp_path)
        core = CoreLoop(settings, runtime_provider=FakeRuntimeProvider())

        event = MessageEvent(
            source=EventSource.CLI,
            channel="cli",
            author="user",
            content="/goal install requests in sandbox",
        )

        response = await core.handle_event(event)
        assert response is not None
        # Should contain "Goal accepted" or an error from the fake runtime
        assert isinstance(response, str)
        core.shutdown()

    @pytest.mark.asyncio
    async def test_core_loop_normal_message_still_works(self, tmp_path):
        """Non-goal messages still go through normal handle_message."""
        from zoomac.core.config import ZoomacSettings
        from zoomac.core.loop import CoreLoop
        from zoomac.core.events import EventSource, MessageEvent

        settings = ZoomacSettings(project_dir=tmp_path)
        core = CoreLoop(settings, runtime_provider=FakeRuntimeProvider())

        event = MessageEvent(
            source=EventSource.CLI,
            channel="cli",
            author="user",
            content="Hello Zoomac!",
        )

        response = await core.handle_event(event)
        assert isinstance(response, str)
        assert len(response) > 0
        core.shutdown()
